"""Publish only a promoted, CI-verified main commit; never replace a release."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

try:
    from scripts import acl_ci_reuse as reuse
except ImportError:  # запуск как python scripts/acl_publish_release.py
    import acl_ci_reuse as reuse


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True, encoding="utf-8", errors="replace").strip()


def _gh(*args: str) -> str:
    return subprocess.check_output(["gh", *args], text=True, encoding="utf-8", errors="replace").strip()


def _api(path: str) -> dict:
    return json.loads(_gh("api", path))


def verify_promotion(tag: str, source_repo: str, report=print) -> str:
    if not re.fullmatch(r"acl-release-[0-9A-Za-z][0-9A-Za-z._-]{0,63}", tag):
        raise ValueError("Expected an acl-release-* tag")
    ref = f"refs/tags/{tag}"
    if _git("cat-file", "-t", ref) != "tag":
        raise ValueError("A release requires an annotated tag")
    sha = _git("rev-parse", f"{ref}^{{commit}}")
    if sha != _git("rev-parse", "origin/main") or sha != _git("rev-parse", "HEAD"):
        raise ValueError("The tag, checkout and current origin/main must name the same commit")
    annotation = _git("cat-file", "-p", ref)
    if not re.search(r"\bsuccess\b", annotation, re.I):
        raise ValueError("Tag annotation must record successful CI")
    match = re.search(r"https://github\.com/" + re.escape(source_repo) + r"/actions/runs/(\d+)\b", annotation)
    if not match:
        raise ValueError("Tag annotation must link to this repository's successful CI run")
    run = json.loads(_gh("api", f"repos/{source_repo}/actions/runs/{match[1]}"))
    if not (run.get("head_sha") == sha and run.get("status") == "completed"
            and run.get("conclusion") == "success" and run.get("path") == ".github/workflows/ci.yml"
            and run.get("head_branch") == "main" and run.get("event") in {"push", "workflow_dispatch"}):
        raise ValueError("Recorded CI must be a successful full main run on the exact release commit")
    # ACL-1565: main-прогон может переиспользовать полный зелёный прогон того же
    # дерева (тесты пропущены). Принимаем его, только если полный прогон доказуем.
    run = dict(run, id=int(match[1]))
    main_url = f"https://github.com/{source_repo}/actions/runs/{run['id']}"
    # Ошибка API на заданиях самого main-прогона не глотается: релиз останавливается.
    jobs = reuse.latest_jobs(_api, source_repo, run)
    # ACL-1582: прогон старой раскладки (всё на Windows, `test (py, shard)`) не
    # доказывает текущий набор заданий — ни сам, ни через переиспользование.
    if reuse.legacy_layout(jobs, "ci.yml"):
        raise ValueError("Recorded main CI used the job layout before ACL-1582 (test (py, shard)); "
                         "rerun ci.yml on main to get a run with the Linux and Windows test jobs")
    if reuse.jobs_complete(jobs, "ci.yml"):
        report(f"Full test matrix ran in the recorded main run {main_url}")
        return sha
    tree = _git("rev-parse", f"{sha}^{{tree}}")
    try:
        # ACL-1649: полный набор (3.11 + 3.13 + Windows) может быть собран из нескольких прогонов
        # этого дерева — stage, PR в main — и из прогона PR, доказуемо связанного с main-коммитом.
        # ACL-2107: запросы повторяются; ошибка commits/pulls после повторов - «не доказано», не «нет PR».
        api = reuse.retrying(_api)
        heads = reuse.collect_pr_heads(Path.cwd(), api, source_repo, tree, [sha], strict=True)
        # Прогоны, которые main-прогон сам назвал переиспользованными, берём по id напрямую.
        logs = reuse.retrying(lambda job_id: _gh("api", "--allow-escape-sequences",
                                               f"repos/{source_repo}/actions/jobs/{job_id}/logs"))
        named = reuse.reused_run_ids(api, source_repo, jobs, "ci.yml", logs=logs, tree=tree)
        # Собственные пройденные задания записанного main-прогона тоже считаются (3.11 после
        # PR в main идёт именно в нём): он push-прогон этого коммита, поэтому не исключается.
        proof = reuse.find_reuse(Path.cwd(), api, source_repo, "ci.yml", tree, pr_heads=heads,
                                 run_ids=named + [run["id"]], empty_retries=2)
    except Exception as error:  # noqa: BLE001 — недоказанный прогон = отказ
        raise ValueError("Recorded main CI reused a run that could not be proven: " + type(error).__name__) from error
    if not proof:
        raise ValueError("Recorded main CI neither ran the full test matrix nor reused "
                         "a provable successful full run of the same tree")
    # В журнал релиза — запуск, который на самом деле прогнал тесты этого дерева.
    urls = [r["run_url"] for r in proof.get("runs") or [proof]]
    report(f"Main run {main_url} reused full results of {'run' if len(urls) == 1 else 'runs'} {', '.join(urls)} "
           f"(commit {proof['commit']}, same tree {tree})")
    return sha


def publish(tag: str, repo: str, directory: Path) -> None:
    # An API failure is not evidence that a release is absent. Inspect the list
    # successfully before creating anything, and fail if this tag already exists.
    existing = json.loads(_gh("release", "list", "--repo", repo, "--limit", "1000", "--json", "tagName"))
    if any(item["tagName"] == tag for item in existing):
        raise ValueError("This release tag already exists; never overwrite it. Resolve an unfinished draft or promote a new tag.")
    manifest = json.loads((directory / "release.json").read_text(encoding="utf-8"))
    if manifest["tag"] != tag:
        raise ValueError("Manifest belongs to a different release tag")
    assets = [directory / manifest["installer"]["name"],
              directory / (manifest["installer"]["name"] + ".sha256"), directory / "release.json"]
    if not all(p.is_file() for p in assets):
        raise ValueError("Release assets are incomplete")
    # ACL-1357: подпись издателя едет рядом с release.json, если шаг подписи её
    # сделал; обязательность решает сам шаг подписи (--require).
    signature = directory / "release.json.sig"
    if signature.is_file():
        assets.append(signature)
    # Upload first to a draft. /releases/latest cannot expose a half upload.
    _gh("release", "create", tag, *(str(p) for p in assets), "--repo", repo, "--draft",
        "--title", f"Adopy Campaign Manager {manifest['version']}",
        "--notes-file", str(directory / "release-notes.md"))
    _gh("release", "edit", tag, "--repo", repo, "--draft=false", "--latest")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--source-repo")
    parser.add_argument("--repo")
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    if args.source_repo:
        verify_promotion(args.tag, args.source_repo)
    elif args.repo and args.directory:
        publish(args.tag, args.repo, args.directory)
    else:
        parser.error("Provide --source-repo to verify, or --repo and --directory to publish")


if __name__ == "__main__":
    main()
