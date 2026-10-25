"""Turn a live robot capture into a scrubbed bench fixture (ACL-1433).

A live failure leaves `output/rpa_pages/<run>/NN_<step>/` on the machine that
ran it: `page.html` (the whole editor, capped at ~4 MB and often cut mid-node)
or `block_<name>.html` (one block the step asked for), plus `context.json`
(what the robot saw: step, run, campaign, diag). Those files are private
diagnostics. This script cuts the relevant block(s) out of a COPY of the page,
strips everything that is not structure and scrubs what identifies an account,
and writes a fixture under `tests/fixtures/ads_manager/` that may be committed
after review.

    python scripts/capture_to_fixture.py <capture_dir> --name placements_doc19 \
        --fragment "card=[role=heading]|text=Account controls|smallest" \
        [--fragment NAME=SPEC ...] [--replace "Real Page=Sample Page"] \
        [--source block_creative_wizard.html] [--stub tests/test_bench_x.py]

SPEC is a CSS selector, optionally followed by `|`-separated modifiers:

- `text=<s>`  keep only matches whose text contains <s> (repeatable);
- `smallest`  of the matches, the one with the shortest text (the default
  when a `text=` is given), otherwise the first;
- `nth=<k>`   the k-th match (0-based) instead;
- `up=<n>`    then climb n ancestors.

What is kept: tags, text and the attributes the robot reads (`role`, `aria-*`
except `aria-activedescendant`, `id`, `data-surface`, `placeholder`, `type`,
`value`, `tabindex`, `hidden`, `for`, `checked`, `disabled`, `name`, `min`,
`max`, `step`, `typeaheadcomponentid`, `contenteditable`; `href` set to `#`). Removed: scripts,
styles, frames, images/video (an `<i>` stands in), SVG (an `<i>`), classes,
inline styles, `data-*` other than `data-surface` and `data-test-decoration` (Meta's
selected-tile mark, which the robot reads), the robot's own marks.
Meta render ids (`js_*`, `_r_*_`, `u_*`, `mount_*`) become `syn_N`, the same N
wherever the same id is referenced (`for`, `aria-labelledby`, ...).

What is scrubbed in text and attribute values: the campaign name from
`context.json`, `--replace` pairs, e-mail addresses, URLs, `act=`/`act_`
account ids, digit runs of 9+ (object ids → synthetic `1000000000000NN`),
phone numbers, tokens (long base64/hex-like strings, `EAA…`, JWT), UUIDs, and
media file names (→ `sample_media_NN.<ext>`). The result is then checked with
the rules of `scripts/acl_scan_secrets.py`; a finding refuses to write.

A green run is NOT proof the fixture is clean: Page names, form names, ad copy
and interests are ordinary words and pass every regex. `--review` prints every
distinct text the fixture carries — read it, add `--replace` pairs, rerun.
The README of the fixtures folder has the full procedure.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts._console import use_utf8  # noqa: E402

FIXTURES = _ROOT / "tests" / "fixtures" / "ads_manager"

KEEP_ATTRS = (
    "role", "id", "data-surface", "placeholder", "type", "value", "tabindex", "hidden",
    "for", "checked", "disabled", "name", "min", "max", "step", "typeaheadcomponentid",
    "contenteditable", "data-test-decoration",
)

#: JS that cuts one fragment out of the loaded capture (runs in offline
#: Chromium with page scripts disabled). Returns {found, html, matches, text}.
_CUT_JS = r"""
({selector, texts, nth, smallest, up, keep}) => {
  const norm = (s) => (s || '').replace(/[\s​]+/g, ' ').trim();
  let all;
  try { all = [...document.querySelectorAll(selector)]; }
  catch (e) { return { found: false, error: 'bad selector: ' + e.message }; }
  let cands = all.filter(e => texts.every(t => norm(e.textContent).includes(t)));
  const matches = cands.length;
  if (!cands.length) return { found: false, matches: 0, all: all.length };
  let el;
  if (nth !== null && nth !== undefined) el = cands[nth];
  else if (smallest) el = cands.sort((a, b) => norm(a.textContent).length - norm(b.textContent).length)[0];
  else el = cands[0];
  if (!el) return { found: false, matches };
  for (let i = 0; i < up && el.parentElement; i++) el = el.parentElement;
  const root = el.cloneNode(true);
  const drop = 'script,style,link,meta,iframe,frame,object,embed,noscript,template,canvas,audio,source,track';
  for (const x of root.querySelectorAll(drop)) x.remove();
  for (const x of [...root.querySelectorAll('svg,img,video,picture')]) {
    if (!x.parentNode) continue;              // inside an svg already replaced
    x.replaceWith(document.createElement('i'));
  }
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_COMMENT);
  const comments = [];
  for (let c = walker.nextNode(); c; c = walker.nextNode()) comments.push(c);
  comments.forEach(c => c.remove());
  const ids = new Map();
  const syn = (tok) => {
    if (!/^(js_[0-9a-z]+|_r_[0-9a-z]+_|u_[0-9a-z_]+|mount_[0-9_]+|[0-9a-f]{8,}(?:-[0-9]+)?|[0-9]+)$/i.test(tok)) return tok;
    if (!ids.has(tok)) ids.set(tok, 'syn_' + (ids.size + 1));
    return ids.get(tok);
  };
  const idRefs = ['id', 'for', 'name', 'aria-labelledby', 'aria-describedby', 'aria-controls', 'aria-owns',
                  'aria-details', 'aria-errormessage', 'aria-flowto'];
  for (const n of [root, ...root.querySelectorAll('*')]) {
    for (const a of [...n.attributes]) {
      const name = a.name;
      const ok = keep.includes(name) || name === 'href'
        || (name.startsWith('aria-') && name !== 'aria-activedescendant');
      // data-test-decoration is kept only as Meta's selected-tile mark.
      if (!ok || (name === 'data-test-decoration' && a.value !== 'selected')) { n.removeAttribute(name); continue; }
      if (name === 'href') { n.setAttribute('href', '#'); continue; }
      if (idRefs.includes(name)) {
        const v = a.value.split(/\s+/).filter(Boolean).map(syn).join(' ');
        if (v) n.setAttribute(name, v); else n.removeAttribute(name);
      }
    }
  }
  return { found: true, matches, html: root.outerHTML, text: norm(root.textContent).slice(0, 160) };
}
"""

_MEDIA_EXT = r"(?:jpe?g|png|gif|webp|heic|bmp|mp4|mov|m4v|avi|webm|mkv)"
_TLD = (r"(?:com|net|org|io|co|info|xyz|online|site|app|biz|me|top|club|store|shop|live|pro"
        r"|ru|ua|br|mx|ar|cl|pe|gt|sa|ae|in|uk|de|es|fr|it)")
_META_HOSTS = ("facebook.com", "instagram.com", "meta.com", "fb.com", "whatsapp.com",
               "messenger.com", "threads.net", "example.com")


@dataclass
class Scrubber:
    """Text replacements, stable within one fixture (same input → same output)."""

    pairs: list[tuple[str, str]] = field(default_factory=list)
    _media: dict[str, str] = field(default_factory=dict)
    _numbers: dict[str, str] = field(default_factory=dict)

    def _media_name(self, m: re.Match) -> str:
        name = m.group(0)
        if name.lower().startswith("sample_media_"):
            return name
        if name not in self._media:
            ext = name.rsplit(".", 1)[-1].lower()
            self._media[name] = f"sample_media_{len(self._media) + 1:02d}.{ext}"
        return self._media[name]

    def _number(self, m: re.Match) -> str:
        digits = m.group(0)
        if set(digits) == {"0"}:          # placeholders (the zero UUID) stay
            return digits
        if digits not in self._numbers:
            self._numbers[digits] = f"1000000000000{len(self._numbers) + 1:02d}"
        return self._numbers[digits]

    @staticmethod
    def _domain(m: re.Match) -> str:
        host = m.group(0).lower()
        if host in _META_HOSTS or any(host.endswith("." + h) for h in _META_HOSTS):
            return m.group(0)
        return "example.com"

    def __call__(self, text: str) -> str:
        # ACL-1358: the values of this machine's own tokens go first, whatever
        # shape they have; the patterns below only know a few token formats.
        try:
            from config import token_vault

            for secret in sorted(token_vault.configured_secret_values(), key=len, reverse=True):
                text = text.replace(secret, "TOKEN_REDACTED")
        except Exception:
            pass
        for real, synthetic in sorted(self.pairs, key=lambda p: -len(p[0])):
            if real:
                text = text.replace(real, synthetic)
        # Order matters: URLs before e-mails/ids so a URL is replaced whole.
        text = re.sub(r"https?://[^\s\"'<>]+", "https://example.com/", text)
        text = re.sub(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b", "user@example.com", text)
        text = re.sub(r"act([_=])\d+", r"act\g<1>1234567890", text)
        # The business scope in Meta's surface names carries the person's name
        # (`/am/business_scope:personal:<id>:<First Last>`).
        text = re.sub(r"business_scope:[^/\"'<>]*", "business_scope:personal:0:Sample Person", text)
        # Profile/account aliases Meta shows as dataset or page names
        # ("Pixel Facebook_6540", "FacebookDoc19").
        text = re.sub(r"\bFacebook_?(?:Doc)?\d{2,6}\b", "Sample Account", text)
        text = re.sub(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
            "00000000-0000-0000-0000-000000000000", text, flags=re.I)
        text = re.sub(r"\bEAA[A-Za-z0-9]{20,}", "TOKEN_REDACTED", text)
        text = re.sub(r"\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]+)?", "TOKEN_REDACTED", text)
        # Long opaque strings: hashes, base64 blobs, session keys. A word of
        # 32+ characters mixing letters and digits is never a UI label.
        # Meta's own snake_case surface names (`ads_sfcrop_tool_..._ef10`) are
        # long too, so separators end a token here.
        text = re.sub(r"\b(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{32,}\b",
                      "TOKEN_REDACTED", text)
        text = re.sub(r"(?<![\w/+])[A-Za-z0-9+/]{40,}={1,2}", "TOKEN_REDACTED", text)
        text = re.sub(rf"[\w()\[\]+.-]+\.{_MEDIA_EXT}(?![A-Za-z0-9])", self._media_name,
                      text, flags=re.I)
        # Bare landing/tracking domains in ad previews ("tkr.example-offer.com").
        text = re.sub(rf"(?<![\w@/.-])(?:[A-Za-z0-9-]+\.)+{_TLD}(?![\w.-])", self._domain, text)
        text = re.sub(r"\+\d[\d\s().-]{7,}\d", "+10000000000", text)
        # Object ids also sit after "_" or "-" (`action-menu_<id>`,
        # `nux-anchor-<id>`), so only letters, digits and "." end a run.
        text = re.sub(r"(?<![0-9A-Za-z.])\d{9,}(?![0-9A-Za-z])", self._number, text)
        return text


def secret_findings(text: str) -> list[str]:
    """The rules of scripts/acl_scan_secrets.py applied to one text."""
    from scripts.acl_scan_secrets import _PLACEHOLDERS, _RULES

    found = []
    for name, pattern, _why in _RULES:
        for m in re.finditer(pattern, text):
            if m.group(0) not in _PLACEHOLDERS:
                found.append(f"{name}: {m.group(0)[:24]}")
    return found


@dataclass
class Spec:
    name: str
    selector: str
    texts: list[str] = field(default_factory=list)
    nth: int | None = None
    smallest: bool = False
    up: int = 0


def parse_fragment(arg: str) -> Spec:
    if "=" not in arg:
        raise ValueError(f"--fragment wants NAME=SPEC, got {arg!r}")
    name, spec = arg.split("=", 1)
    name = name.strip()
    if not re.fullmatch(r"\w+", name):
        raise ValueError(f"fragment name must be a word (letters, digits, _): {name!r}")
    parts = spec.split("|")
    out = Spec(name=name, selector=parts[0].strip())
    for p in parts[1:]:
        p = p.strip()
        if p.startswith("text="):
            out.texts.append(p[5:])
            out.smallest = True
        elif p == "smallest":
            out.smallest = True
        elif p == "first":
            out.smallest = False
        elif p.startswith("nth="):
            out.nth = int(p[4:])
        elif p.startswith("up="):
            out.up = int(p[3:])
        elif p:
            raise ValueError(f"unknown modifier {p!r} in {arg!r}")
    return out


def normalise_name(name: str) -> str:
    """`Placements Doc19` → `placements_doc19` (lower snake case)."""
    s = re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower()
    if not s:
        raise ValueError("empty fixture name")
    return s


def capture_date(ctx: dict) -> str:
    ts = str(ctx.get("ts") or "")
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", ts)
    return "_".join(m.groups()) if m else "unknown_date"


def pick_source(capture: Path, source: str | None) -> Path:
    if source:
        p = capture / source
        if not p.exists():
            raise FileNotFoundError(p)
        return p
    if (capture / "page.html").exists():
        return capture / "page.html"
    blocks = sorted(capture.glob("block_*.html"))
    if blocks:
        return blocks[0]
    raise FileNotFoundError(f"no page.html or block_*.html in {capture}")


def cut(html: str, specs: list[Spec], *, browser_executable: str | None = None) -> list[dict]:
    """Cut every fragment out of `html` in offline, script-less Chromium."""
    from playwright.sync_api import sync_playwright

    out = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=browser_executable or None)
        try:
            ctx = browser.new_context(java_script_enabled=False)
            ctx.route("**/*", lambda route: route.abort())
            page = ctx.new_page()
            page.set_content(html, wait_until="domcontentloaded")
            for spec in specs:
                res = page.evaluate(_CUT_JS, {
                    "selector": spec.selector, "texts": spec.texts, "nth": spec.nth,
                    "smallest": spec.smallest, "up": spec.up, "keep": list(KEEP_ATTRS),
                })
                res["name"] = spec.name
                out.append(res)
        finally:
            browser.close()
    return out


def build_fixture(capture: Path, specs: list[Spec], *, source: str | None = None,
                  replace: list[tuple[str, str]] | None = None,
                  browser_executable: str | None = None) -> tuple[str, dict]:
    """Return (fixture text, info). Raises ValueError when a fragment is missing
    or the result still carries something the secret scanner flags."""
    ctx_path = capture / "context.json"
    ctx = json.loads(ctx_path.read_text(encoding="utf-8")) if ctx_path.exists() else {}
    src = pick_source(capture, source)
    raw = src.read_text(encoding="utf-8", errors="replace")
    pairs = list(replace or [])
    campaign = str(ctx.get("campaign") or "").strip()
    if campaign:
        pairs.append((campaign, "Sample Campaign 01"))
    scrub = Scrubber(pairs=pairs)
    cuts = cut(raw, specs, browser_executable=browser_executable)
    missing = [c["name"] for c in cuts if not c.get("found")]
    if missing:
        detail = {c["name"]: {k: c.get(k) for k in ("matches", "all", "error")} for c in cuts
                  if not c.get("found")}
        raise ValueError(f"fragment(s) not found in {src.name}: {missing} {detail}")
    run = capture.parent.name
    step_dir = capture.name
    truncated = "yes" if ctx.get("html_truncated") else "no"
    if src.name != "page.html":
        truncated = "no (block capture)"
    parts = [
        f"<!-- bench fixture: step {ctx.get('step') or step_dir.split('_', 1)[-1]} | "
        f"source {run}/{step_dir}/{src.name} | captured {ctx.get('ts') or 'unknown'} | "
        f"truncated {truncated} | UI language, Meta build and browser version not recorded | "
        f"cut by scripts/capture_to_fixture.py -->"
    ]
    for c in cuts:
        body = scrub(c["html"])
        parts.append(f"<!-- fragment: {c['name']} | source: {run}/{step_dir} -->\n{body}")
    text = "\n".join(parts) + "\n"
    found = secret_findings(text)
    if found:
        raise ValueError("the fixture still carries identifiers: " + "; ".join(found))
    info = {"step": ctx.get("step"), "run": run, "capture": step_dir, "source": src.name,
            "date": capture_date(ctx), "truncated": bool(ctx.get("html_truncated")),
            "fragments": {c["name"]: c.get("matches") for c in cuts}}
    return text, info


def review_texts(fixture_text: str) -> list[str]:
    """Every distinct visible text and attribute value a reviewer must read."""
    body = re.sub(r"<!--.*?-->", " ", fixture_text, flags=re.S)
    texts = {t.strip() for t in re.split(r"<[^>]+>", body) if t.strip()}
    attrs = {m.group(2) for m in re.finditer(r'\s(aria-label|placeholder|value|data-surface)="([^"]*)"', body)}
    return sorted(texts | attrs)


STUB = '''# -*- coding: utf-8 -*-
"""{step}: the layout `{name}` (live capture {run}/{capture}, {date}).

Stub written by scripts/capture_to_fixture.py. Replace the TODO with the
robot's real step function (or its JS reader) on this fragment and the outcome
the step must have here; add the row to tests/bench/catalogue.json.
"""
from __future__ import annotations

import pytest

from tests.bench_support import FIXTURES, fragment, offline_page  # noqa: F401

FIXTURE = FIXTURES / "{file}"


@pytest.mark.parametrize("name", {fragments!r})
def test_{name}_fragment_loads(offline_page, name):
    offline_page.set_content(fragment(FIXTURE, name))
    assert offline_page.locator("body *").count() > 0


def test_{name}_outcome(offline_page):
    offline_page.set_content(fragment(FIXTURE, {first!r}))
    raise NotImplementedError("TODO: run the robot's step on this layout and assert its outcome")
'''


def write_stub(path: Path, *, name: str, file: str, info: dict) -> None:
    path.write_text(STUB.format(
        step=info.get("step") or "step", name=name, run=info["run"], capture=info["capture"],
        date=info["date"], file=file, fragments=list(info["fragments"]),
        first=next(iter(info["fragments"]))), encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    use_utf8()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("capture", type=Path, help="output/rpa_pages/<run>/NN_<step> folder")
    ap.add_argument("--name", required=True, help="layout name, e.g. placements_doc19")
    ap.add_argument("--fragment", action="append", required=True, metavar="NAME=SPEC")
    ap.add_argument("--source", help="file in the capture folder (default page.html, else block_*.html)")
    ap.add_argument("--replace", action="append", default=[], metavar="REAL=SYNTHETIC")
    ap.add_argument("--out", type=Path, default=FIXTURES)
    ap.add_argument("--date", help="override the capture date (YYYY_MM_DD)")
    ap.add_argument("--stub", type=Path, help="also write a test stub there")
    ap.add_argument("--review", action="store_true", help="print every text for manual review")
    ap.add_argument("--dry-run", action="store_true", help="print, do not write")
    ap.add_argument("--append", action="store_true",
                    help="add the fragments to an existing fixture of the same name and date")
    ap.add_argument("--force", action="store_true", help="overwrite an existing fixture")
    args = ap.parse_args(argv)

    try:
        specs = [parse_fragment(f) for f in args.fragment]
        pairs = []
        for r in args.replace:
            if "=" not in r:
                raise ValueError(f"--replace wants REAL=SYNTHETIC, got {r!r}")
            pairs.append(tuple(r.split("=", 1)))
        text, info = build_fixture(args.capture, specs, source=args.source, replace=pairs)
    except (ValueError, FileNotFoundError) as exc:
        print(f"capture_to_fixture: {exc}", file=sys.stderr)
        return 1

    name = normalise_name(args.name)
    file = f"{name}_{args.date or info['date']}.html"
    if args.review:
        for t in review_texts(text):
            print(f"  | {t}")
    if args.dry_run:
        print(text)
        return 0
    args.out.mkdir(parents=True, exist_ok=True)
    target = args.out / file
    if args.append and target.exists():
        old = target.read_text(encoding="utf-8")
        clash = set(re.findall(r"<!-- fragment: (\w+) ", old)) & set(info["fragments"])
        if clash:
            print(f"capture_to_fixture: {file} already has fragment(s) {sorted(clash)}", file=sys.stderr)
            return 1
        text = old.rstrip("\n") + "\n" + text
    elif target.exists() and not args.force:
        print(f"capture_to_fixture: {target} exists (use --append to add fragments, "
              f"--force to overwrite)", file=sys.stderr)
        return 1
    target.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {args.out / file} ({len(text.encode('utf-8'))} bytes, fragments {info['fragments']})")
    if args.stub:
        write_stub(args.stub, name=name, file=file, info=info)
        print(f"wrote stub {args.stub}")
    print("Now: read the texts (--review), run scripts/acl_scan_secrets.py after `git add`, "
          "describe the fixture in tests/fixtures/ads_manager/README.md and tests/bench/catalogue.json.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
