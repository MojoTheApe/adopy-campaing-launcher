"""Seed the team library repository from this machine's targets and ad texts (ACL-1241).

Run once on the machine whose library becomes the team's starting point:

    python scripts/seed_team_library.py --dry-run
    python scripts/seed_team_library.py

An already seeded library is left alone. On another machine, ``--add-missing``
uploads only what the team library does not have yet: a name that is neither in
the library nor in Recently deleted, and not the same set of values under
another name.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import paths  # noqa: E402
from src.analysis_agent import ad_text_registry as ar  # noqa: E402
from src.analysis_agent import targeting_registry as tr  # noqa: E402
from src.analysis_agent import team_library as tl  # noqa: E402
from src.fb_api import form_templates as ft  # noqa: E402

KINDS = {
    "targets": (tr.snapshot, "targets", tr.normalise_one,
                lambda r: tr.fingerprint(r.get("interests", ""), r.get("narrow", ""))),
    "ad_texts": (ar.snapshot, "texts", ar.normalise_one,
                 lambda r: ar.fingerprint(r.get("headline", ""), r.get("body", ""), r.get("description", ""))),
    # ACL-2054: ключ записи — id шаблона; «отпечаток» — имя и содержание.
    "form_templates": (ft.snapshot, "templates", str.lower,
                       lambda r: str(r.get("name", "")).casefold() + ft.content_hash(ft.validate(r)[0])),
}


def plan(kind: str, remote: dict, local: dict, add_missing: bool) -> tuple[dict, list[str]]:
    """What to upload and why the rest is skipped."""
    _, key, norm, mark = KINDS[kind]
    items, skipped = {}, []
    if (remote[key] or remote.get("deleted") or remote.get("renamed")) and not add_missing:
        return {}, [f"the team library already has {len(remote[key])} {kind} and may have deletion/rename history; use --add-missing"]
    taken = {norm(n) for n in remote[key]} | {norm(n) for n in remote.get("deleted", {})}
    marks = {mark(r) for r in remote[key].values()}
    for name, rec in sorted(local[key].items()):
        if norm(name) in taken:
            skipped.append(f"{name}: already in the team library or Recently deleted")
        elif mark(rec) in marks:
            skipped.append(f"{name}: the same values already exist under another name")
        else:
            items[name] = rec
            taken.add(norm(name))
            marks.add(mark(rec))
    return items, skipped


def seed(kind: str, *, add_missing: bool, dry_run: bool) -> tuple[int, list[str]]:
    snapshot, key, _, _ = KINDS[kind]
    local = snapshot()
    if dry_run:
        items, skipped = plan(kind, tl._parse(kind, tl.backend().get(tl.FILES[kind][0])), local, add_missing)
        return len(items), skipped
    report: dict = {}

    def change(remote):
        items, skipped = plan(kind, remote, local, add_missing)
        stamp = {"added_by": tl.author()}
        for name, rec in items.items():
            remote[key][name] = {**rec, **{k: v for k, v in stamp.items() if not rec.get(k)}}
        report["n"], report["skipped"] = len(items), skipped

    tl.transact(kind, change, f"Seed {kind} from {tl.author()}", allow_create=True)
    return report["n"], report["skipped"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--add-missing", action="store_true", help="upload only what the team library lacks")
    ap.add_argument("--dry-run", action="store_true", help="show what would be uploaded, change nothing")
    args = ap.parse_args(argv)
    paths.load_env()
    if not tl.configured():
        print("The team library is not connected: set ACL_LIBRARY_REPO and ACL_LIBRARY_TOKEN "
              f"in {paths.get_machine_env_path()}.", file=sys.stderr)
        return 2
    for kind in KINDS:
        try:
            n, skipped = seed(kind, add_missing=args.add_missing, dry_run=args.dry_run)
        except tl.LibraryError as exc:
            print(f"{kind}: {exc}", file=sys.stderr)
            return 1
        print(f"{kind}: {'would upload' if args.dry_run else 'uploaded'} {n}")
        for line in skipped:
            print(f"  skipped {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
