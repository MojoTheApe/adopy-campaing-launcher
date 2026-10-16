#!/usr/bin/env python3
"""Emit native delivery receipt only after the actual published acceptance passes."""
import argparse
import json
from pathlib import Path


def evidence(request, report, run_url):
    published = report.get("published", {})
    current = published.get("current", {})
    if (report.get("result") != "passed" or report.get("source_sha") != request["merge_sha"]
            or report.get("tree_sha") != request["tree_sha"]
            or current.get("commit") != request["merge_sha"] or current.get("tag") != request["tag"]
            or report.get("update", {}).get("result") != "updated"
            or report.get("recovery", {}).get("result") != "rolled_back"
            or len(report.get("checks", [])) < 5):
        raise ValueError("Published install/update/restart/recovery acceptance is incomplete or stale")
    return {"kind": "delivery-verification", "result": "passed", "target": request["target"],
            "merge_shas": [request["merge_sha"]],
            "checks": ["Published manifest, configured updater trust, installer SHA-256 and size verified",
                       *report["checks"]],
            "report": run_url + "; " + published["release_url"],
            "release": {"tag": current["tag"], "repository": published["repository"],
                        "installer_sha256": current["installer"]["sha256"]}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--artifact-name", required=True)
    args = parser.parse_args()
    result = evidence(json.loads(args.request.read_text()), json.loads(args.report.read_text()), args.run_url)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("TRACKER_DELIVERY_RECEIPT " + json.dumps(result, separators=(",", ":")))
    print("TRACKER_DELIVERY_ARTIFACT " + json.dumps({"name": args.artifact_name}, separators=(",", ":")))


if __name__ == "__main__":
    main()
