"""Render the bench catalogue (tests/bench/catalogue.json) to tests/bench/CATALOGUE.md.

    python scripts/bench_catalogue.py            # print the Markdown
    python scripts/bench_catalogue.py --write    # rewrite CATALOGUE.md
    python scripts/bench_catalogue.py --check    # exit 1 if CATALOGUE.md is stale

The JSON is the source; the Markdown is what people read. tests/test_bench_catalogue.py
fails when they drift apart or when a fixture, fragment or test named in the
catalogue does not exist (ACL-1433).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts._console import use_utf8  # noqa: E402

CATALOGUE_JSON = _ROOT / "tests" / "bench" / "catalogue.json"
CATALOGUE_MD = _ROOT / "tests" / "bench" / "CATALOGUE.md"


def load() -> dict:
    return json.loads(CATALOGUE_JSON.read_text(encoding="utf-8"))


def _cell(text) -> str:
    return str(text if text is not None else "—").replace("|", "\\|").replace("\n", " ")


def render(cat: dict) -> str:
    steps = cat["steps"]
    layouts = [lay for s in steps for lay in s["layouts"]]
    fixtures = sorted({lay["fixture"] for lay in layouts if lay.get("fixture")})
    xfails = [(s["step"], lay) for s in steps for lay in s["layouts"] if lay.get("status") == "xfail"]
    out = [
        "# Bench catalogue — every Meta layout the robot has met",
        "",
        "<!-- Generated from tests/bench/catalogue.json by scripts/bench_catalogue.py --write. "
        "Do not edit by hand. -->",
        "",
        cat["about"],
        "",
        cat["harvested"],
        "",
        f"**{len(steps)} robot steps × {len(layouts)} layouts, {len(fixtures)} fixtures, "
        f"{len(xfails)} known live bugs (xfail).** Fixtures live in `tests/fixtures/ads_manager/` "
        "(how they are cut: its README and `scripts/capture_to_fixture.py`).",
        "",
        "## Summary",
        "",
        "| Step | Layouts | pass | xfail | reference |",
        "| --- | --- | --- | --- | --- |",
    ]
    for s in steps:
        st = [lay.get("status") for lay in s["layouts"]]
        out.append(f"| {_cell(s['step'])} | {len(st)} | {st.count('pass')} | {st.count('xfail')} | "
                   f"{st.count('reference')} |")
    if xfails:
        out += ["", "## Known live bugs (strict xfail — fix in the RPA track)", ""]
        for step, lay in xfails:
            out.append(f"- **{step}** — {lay['layout']} (`{lay['fixture']}`, `{lay['test']}`): "
                       f"{lay.get('bug', '')}")
    for s in steps:
        out += ["", f"## {s['step']}", "", f"Robot: {s['robot']}", "",
                "| Layout | Seen live | Fixture · fragment | Test | Expected | Status |",
                "| --- | --- | --- | --- | --- | --- |"]
        for lay in s["layouts"]:
            fx = lay.get("fixture")
            where = f"`{fx}`" + (f" · {lay['fragment']}" if lay.get("fragment") else "") if fx else "—"
            out.append("| " + " | ".join([
                _cell(lay["layout"]), _cell(", ".join(lay.get("seen") or [])), _cell(where),
                f"`{lay['test']}`", _cell(lay["expected"]), _cell(lay["status"]),
            ]) + " |")
    out += ["", "## Seen but not cut", "", "| Captures | Why not a fixture |", "| --- | --- |"]
    for nc in cat.get("not_cut", []):
        out.append(f"| {_cell(nc['captures'])} | {_cell(nc['why'])} |")
    out += ["", "## Adding a layout", "",
            "Every robot fix adds the live capture that motivated it (review checklist, "
            "`.github/pull_request_template.md`):", "",
            "1. `python scripts/capture_to_fixture.py output/rpa_pages/<run>/NN_<step> --name <layout> "
            "--fragment \"<name>=<css>\" --review --stub tests/test_bench_<layout>.py` "
            "(read the review list, add `--replace` pairs, rerun).",
            "2. Turn the stub into a test that runs the robot's real step function or reader on the "
            "fragment and asserts the outcome; a current robot bug is a strict `xfail` with its reason.",
            "3. Add the layout to `tests/bench/catalogue.json`, run `python scripts/bench_catalogue.py "
            "--write`, describe the fixture in the fixtures README, `git add` it and run "
            "`python scripts/acl_scan_secrets.py`.", ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    use_utf8()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    text = render(load())
    if args.check:
        current = CATALOGUE_MD.read_text(encoding="utf-8") if CATALOGUE_MD.exists() else ""
        if current != text:
            print("tests/bench/CATALOGUE.md is stale: run python scripts/bench_catalogue.py --write")
            return 1
        print("CATALOGUE.md is up to date.")
        return 0
    if args.write:
        CATALOGUE_MD.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {CATALOGUE_MD}")
        return 0
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
