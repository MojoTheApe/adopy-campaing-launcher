"""Download exact published packages through the maintained updater trust path."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]


def validate_current(manifest, release, request):
    if (release.get("draft") or release.get("prerelease")
            or manifest.get("tag") != request["tag"] or release.get("tag_name") != request["tag"]
            or manifest.get("commit") != request["merge_sha"]
            or manifest.get("version") != request["tag"].removeprefix("acl-release-")):
        raise ValueError("The published channel is not the requested exact stable release")
    config = json.loads((ROOT / "tracker/config.json").read_text(encoding="utf-8"))["project"]
    expected = {config.get("legacy_task_ids", {}).get(str(n),
                config["id_prefix"] + "-" + str(n + config.get("id_offset", 0))) for n in request["issues"]}
    if not expected <= {item["issue"] for item in manifest["fixed"]}:
        raise ValueError("The published manifest omits tasks from the authorized batch")


def download_packages(request, artifacts):
    sys.path.insert(0, str(ROOT))
    isolated = artifacts / "published-channel-config"
    if isolated.exists():
        raise ValueError("Inspect the existing isolated channel directory before retry")
    isolated.mkdir(parents=True)
    os.environ["ACL_CONFIG_DIR"] = str(isolated)
    from src.web import release_updates as updates
    channel = updates.channel()
    if not channel:
        raise ValueError("The configured read-only published update channel is unavailable")
    manifest, release = updates.fetch_latest(channel)
    validate_current(manifest, release, request)
    previous_tag = manifest.get("previous_tag")
    if not previous_tag:
        raise ValueError("No previous published package exists; bootstrap acceptance remains pending")
    # The old baseline is fetched by immutable tag, never /latest after it moves.
    previous_release = json.loads(updates._request(
        f"{updates.API}/repos/{channel.repo}/releases/tags/{previous_tag}", channel,
        accept="application/vnd.github+json"))
    if previous_release.get("draft") or previous_release.get("prerelease"):
        raise ValueError("The previous baseline is not a published stable release")
    raw = updates._request(updates._asset(previous_release, "release.json")["url"], channel,
                           accept="application/octet-stream")
    previous_trust = updates.verify_signature(previous_release, raw, channel)
    previous = updates.parse_release(raw)
    baseline = manifest.get("previous_release", {})
    if (baseline.get("tag") != previous_tag or baseline.get("commit") != previous.get("commit")
            or baseline.get("repository") != channel.repo or baseline.get("release_id") != previous_release.get("id")):
        raise ValueError("The baseline differs from the stable publication recorded before release")
    if previous.get("tag") != previous_tag or previous_release.get("tag_name") != previous_tag:
        raise ValueError("The previous published manifest belongs to another tag")
    if not updates.is_newer(manifest["version"], previous["version"]):
        raise ValueError("The published version does not advance the previous package")
    destination = artifacts / "installer"
    destination.mkdir(parents=True, exist_ok=True)
    old = destination / previous["installer"]["name"]
    updates._request(updates._asset(previous_release, old.name)["url"], channel,
                     accept="application/octet-stream", destination=old, timeout=600,
                     limit=previous["installer"]["size"])
    if old.stat().st_size != previous["installer"]["size"] or updates._sha256(old) != previous["installer"]["sha256"]:
        raise ValueError("The published baseline installer hash/size differs")
    original_version = updates.installed_version
    try:
        # Source acceptance has no installed build identity. Only that identity
        # is supplied; the production download, signature/hash/size code runs.
        updates.installed_version = lambda: previous["version"]
        current_file, downloaded = updates.download(channel)
    finally:
        updates.installed_version = original_version
    if downloaded != manifest:
        raise ValueError("The published channel changed during acceptance")
    shutil.copyfile(current_file, destination / manifest["installer"]["name"])
    for entry in (previous, manifest):
        expected_name = f"Adopy-Campaign-Launcher-Setup-{entry['version']}.exe"
        if entry["installer"]["name"] != expected_name:
            raise ValueError("Published package name does not match the maintained installer contract")
        file = destination / expected_name
        if file.stat().st_size != entry["installer"]["size"] or updates._sha256(file) != entry["installer"]["sha256"]:
            raise ValueError("Retained installer differs from the verified published bytes")
    return {"repository": channel.repo, "previous": previous, "current": manifest,
            "previous_signature": previous_trust, "signature": release["_acl_signature"],
            "release_url": release["html_url"], "manifest_sha256": hashlib.sha256(
                updates._request(updates._asset(release, "release.json")["url"], channel,
                                 accept="application/octet-stream")).hexdigest()}
