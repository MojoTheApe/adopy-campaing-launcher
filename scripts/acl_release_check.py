"""Does the release we claim to ship match the commit that actually passed CI?

The audit's wording for F-14: the deployed artifact's SHA must equal the SHA
that passed the build, and the release manifest must record both. Before this,
"release" meant a tag someone put somewhere, and the only evidence that the
code worked was that it had worked on one developer's machine — where every
dependency is already installed, which is precisely what a fresh install is
not.

Checks three things, all locally:

  1. every release named in docs/RELEASES.md points at a commit that exists;
  2. the tag of that name resolves to exactly that commit;
  3. the current `master` is a release recorded in the manifest.

With a GitHub token reachable (the one git already uses on this machine), it
also asks whether the recorded run really is green — so the manifest cannot
claim a build that never passed.

    python scripts/acl_release_check.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts._console import use_utf8  # noqa: E402

use_utf8()

_MANIFEST = _ROOT / "docs" / "RELEASES.md"
_REPO = "MojoTheApe/adopy-campaing-launcher"


def _git(*args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=str(_ROOT), capture_output=True, text=True, timeout=30
    )
    return out.stdout.strip() if out.returncode == 0 else ""


def _releases() -> list[dict]:
    """Parse the manifest. Deliberately tolerant: prose around the tables is
    for people, and a check that breaks when someone adds a paragraph would be
    abandoned within a week."""
    text = _MANIFEST.read_text(encoding="utf-8")
    out: list[dict] = []
    for block in re.split(r"\n## ", text)[1:]:
        name = block.splitlines()[0].strip()
        tag = name.split(" ")[0].strip()
        if not tag.startswith("acl-"):
            continue
        sha = re.search(r"\|\s*Коммит\s*\|\s*`([0-9a-f]{7,40})`", block)
        run = re.search(r"actions/runs/(\d+)", block)
        out.append({
            "tag": tag,
            "superseded": "заменён" in name,
            "sha": sha.group(1) if sha else "",
            "run_id": run.group(1) if run else "",
        })
    return out


def _token() -> str:
    """The token git already uses here. Never printed."""
    try:
        out = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True, text=True, timeout=30, cwd=str(_ROOT),
        )
        for line in out.stdout.splitlines():
            if line.startswith("password="):
                return line.split("=", 1)[1]
    except Exception:
        pass
    return ""


def _run_conclusion(run_id: str, token: str) -> str:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{_REPO}/actions/runs/{run_id}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read()).get("conclusion") or "?"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except Exception as e:
        return f"недоступно ({type(e).__name__})"


def main() -> int:
    if not _MANIFEST.is_file():
        print(f"Нет манифеста релизов: {_MANIFEST}")
        return 1

    releases = _releases()
    if not releases:
        print("В манифесте не нашлось ни одного релиза.")
        return 1

    problems: list[str] = []
    bad_tags: set[str] = set()
    token = _token()
    if not token:
        print("Токен GitHub недоступен — проверю всё, кроме цвета сборки.\n")

    for r in releases:
        tag, sha = r["tag"], r["sha"]
        line = f"{tag}"
        if not sha:
            problems.append(f"{tag}: в манифесте не записан коммит"); bad_tags.add(tag)
            print(f"  [FAIL] {line} — нет коммита в манифесте")
            continue

        if not _git("cat-file", "-e", f"{sha}^{{commit}}") and _git("rev-parse", "--quiet", "--verify", f"{sha}^{{commit}}") == "":
            problems.append(f"{tag}: коммит {sha[:7]} не найден в репозитории"); bad_tags.add(tag)
            print(f"  [FAIL] {line} — коммита {sha[:7]} нет в репозитории")
            continue

        tagged = _git("rev-list", "-n", "1", tag)
        if not tagged:
            problems.append(f"{tag}: метки нет в репозитории"); bad_tags.add(tag)
            print(f"  [FAIL] {line} — метки нет")
            continue
        if not tagged.startswith(sha) and not sha.startswith(tagged[: len(sha)]):
            problems.append(f"{tag}: метка указывает на {tagged[:7]}, а манифест на {sha[:7]}"); bad_tags.add(tag)
            print(f"  [FAIL] {line} — метка {tagged[:7]} ≠ манифест {sha[:7]}")
            continue

        note = ""
        if r["run_id"] and token:
            concl = _run_conclusion(r["run_id"], token)
            note = f", сборка {concl}"
            if concl != "success" and not r["superseded"]:
                problems.append(f"{tag}: записанный прогон не зелёный ({concl})"); bad_tags.add(tag)
        elif not r["run_id"]:
            note = ", прогон сборки не записан"
            if not r["superseded"]:
                problems.append(f"{tag}: прогон сборки не записан"); bad_tags.add(tag)

        # По множеству тегов, а не поиском подстроки: имя старого тега —
        # префикс нового ("…-30" внутри "…-30.2"), и подстрочная проверка
        # помечала заменённый релиз FAIL за чужие проблемы.
        mark = "[FAIL]" if tag in bad_tags else "[ ok ]"
        print(f"  {mark} {line} — {sha[:7]}{note}" + (" (заменён)" if r["superseded"] else ""))

    # Что реально раздаётся: вершина master.
    master = _git("rev-list", "-n", "1", "master")
    current = [r for r in releases if master.startswith(r["sha"]) and r["sha"]]
    print()
    if current:
        print(f"  master = {master[:7]} — это релиз {current[0]['tag']}")
    else:
        problems.append(f"master ({master[:7]}) не записан в манифесте как релиз")
        print(f"  [FAIL] master = {master[:7]} — такого релиза в манифесте нет")

    if problems:
        print("\nРасхождения:")
        for p in problems:
            print(f"  • {p}")
        print("\nПока они есть, «релиз» не значит «проверено».")
        return 1

    print("\nРелиз, метка и прогон сборки сходятся.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
