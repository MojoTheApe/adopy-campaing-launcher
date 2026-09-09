"""After review, mark explicitly selected, fully merged GitHub Issues In master.

Default is a dry run. --apply --reviewed-complete records the reviewer's
confirmation that ALL work for these issues was reviewed and merged. A PR
mention alone cannot establish completeness; the reviewer selects the issues.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from urllib.parse import quote

from .model import load_config
REPOSITORY = load_config()["project"]["repository"]
MERGED = "status:merged"
REVIEW = "status:review"


class GitHub:
    def request(self, path: str, method: str = "GET", payload=None, paginate=False):
        command = ["gh", "api", "--method", method, path]
        if paginate:
            command += ["--paginate", "--slurp"]
        if payload is not None:
            command += ["--input", "-"]
        try:
            result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                                    text=True, capture_output=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError(f"GitHub request failed: {method} {path}") from None
        if result.returncode:
            raise RuntimeError(f"GitHub request failed: {method} {path} (exit {result.returncode})")
        value = json.loads(result.stdout) if result.stdout.strip() else None
        return [item for page in value for item in page] if paginate else value


def unchecked_subtasks(body: str) -> bool:
    fence = None
    for line in body.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            fence = None if fence == marker else marker if fence is None else fence
            continue
        if not fence and re.match(r"^\s*[-*+]\s+\[ \]\s+", line):
            return True
    return False


def related(issue: dict, pr: dict) -> bool:
    text = pr["evidence"]
    if re.search(rf"(?<![\w/])#{issue['number']}(?!\d)", text):
        return True
    url = issue.get("html_url")
    if url and re.search(re.escape(url) + r"(?![\w/])", text):
        return True
    task = re.match(r"^([a-z][a-z0-9]*-\d+[a-z]?)\s*:", issue["title"], re.I)
    if task and re.search(rf"(?<![\w-]){re.escape(task.group(1))}(?![\w-])", text, re.I):
        return True
    branch = pr["head"]["ref"]
    return bool(branch and re.search(rf"(?<![\w/-]){re.escape(branch)}(?![\w/-])", issue.get("body") or ""))


def validate_issue(issue: dict) -> set[str]:
    if "pull_request" in issue:
        raise ValueError(f"#{issue['number']} is a pull request, not a task")
    labels = {label["name"] for label in issue["labels"]}
    statuses = {name for name in labels if name.startswith("status:")}
    if not statuses or not statuses <= {REVIEW, MERGED}:
        raise ValueError(f"#{issue['number']}: expected Review or In master, got {sorted(statuses)}")
    if issue.get("state_reason") == "not_planned":
        raise ValueError(f"#{issue['number']} was closed as not planned")
    if unchecked_subtasks(issue.get("body") or ""):
        raise ValueError(f"#{issue['number']} still has unchecked work; review the task first")
    return labels


def prepare(api: GitHub, repo: str, numbers: list[int], pr_numbers: list[int], base: str = "master") -> list[dict]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("Invalid repository")
    pulls = []
    for number in sorted(set(pr_numbers)):
        pr = api.request(f"repos/{repo}/pulls/{number}")
        if not pr.get("merged") or pr["base"]["ref"] != base:
            raise ValueError(f"PR #{number} is not merged into {base}")
        sha = pr.get("merge_commit_sha") or ""
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError(f"PR #{number} has no verified merge commit")
        comparison = api.request(f"repos/{repo}/compare/{sha}...{quote(base, safe='')}")
        if comparison["status"] not in ("ahead", "identical"):
            raise ValueError(f"PR #{number}'s merge commit is not in current {base}")
        commits = api.request(f"repos/{repo}/pulls/{number}/commits?per_page=100", paginate=True)
        pr["evidence"] = "\n".join([pr.get("title") or "", pr.get("body") or ""] +
                                    [c["commit"]["message"] for c in commits])
        pulls.append(pr)
    if not pulls:
        raise ValueError("At least one merged PR is required")
    plan = []
    for number in sorted(set(numbers)):
        issue = api.request(f"repos/{repo}/issues/{number}")
        labels = validate_issue(issue)
        evidence = [p["number"] for p in pulls if related(issue, p)]
        if not evidence:
            raise ValueError(f"#{number}: no issue ID, ACL ID or source branch links it to the supplied PRs")
        plan.append({"number": number, "title": issue["title"], "prs": evidence,
                     "change": REVIEW in labels or MERGED not in labels})
    return plan


def apply(api: GitHub, repo: str, plan: list[dict]) -> list[int]:
    # Validate the entire current batch again before performing the first write.
    current = {p["number"]: api.request(f"repos/{repo}/issues/{p['number']}") for p in plan}
    for issue in current.values():
        validate_issue(issue)
    api.request(f"repos/{repo}/labels/{quote(MERGED, safe='')}")
    done = []
    for row in plan:
        number = row["number"]
        path = f"repos/{repo}/issues/{number}"
        # Add/remove individual labels, never replace area/track/other labels.
        # Retrying after a partial API failure safely finishes the transition.
        labels = validate_issue(api.request(path))
        if MERGED not in labels:
            api.request(path + "/labels", "POST", {"labels": [MERGED]})
        if REVIEW in labels:
            api.request(path + "/labels/" + quote(REVIEW, safe=""), "DELETE")
        result = api.request(path)
        statuses = {x["name"] for x in result["labels"] if x["name"].startswith("status:")}
        if statuses != {MERGED}:
            raise RuntimeError(f"#{number}: status verification failed; rerun after resolving concurrent edits")
        done.append(number)
        print(f"#{number}: In master (verified)")
    return done


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("issues", nargs="+", type=int, help="Explicitly reviewed and completed issue numbers")
    parser.add_argument("--pr", action="append", required=True, type=int, help="Merged PR evidence; repeat for multiple PRs")
    parser.add_argument("--repo", default=REPOSITORY)
    parser.add_argument("--base", default=load_config()["project"]["base_branch"])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reviewed-complete", action="store_true", help="Reviewer confirms all task work is reviewed and merged")
    args = parser.parse_args(argv)
    if args.apply and not args.reviewed_complete:
        parser.error("--apply requires --reviewed-complete after reviewing ALL work for each selected issue")
    if any(n <= 0 for n in args.issues + args.pr):
        parser.error("Issue and PR numbers must be positive")
    api = GitHub()
    try:
        plan = prepare(api, args.repo, args.issues, args.pr, args.base)
        for row in plan:
            print(f"#{row['number']}: {row['title']} | PRs {row['prs']} | " +
                  ("Review → In master" if row["change"] else "already In master"))
        if args.apply:
            apply(api, args.repo, plan)
            from .github import GitHub as Transport
            from .queue import StateStore, release
            store = StateStore(Transport(args.repo), args.base)
            state, _ = store.read(optional=True)
            if state.get("active") and state["active"]["issue"] in args.issues:
                release(store, state["active"]["issue"])
        else:
            print("Dry run: no GitHub changes")
    except (ValueError, RuntimeError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
