"""Доска задач ACL: источник — файл в Obsidian, отсюда собирается витрина.

Одно хранилище, два вида. Правит человек в Obsidian; скрипт читает тот же
файл и рисует HTML, который можно показать. Ничего своего скрипт не помнит:
если он и разметка разойдутся, прав файл.

    python scripts/acl_board.py --render        собрать доску
    python scripts/acl_board.py --sync          отметить задачи из свежего коммита
    python scripts/acl_board.py --check         разобрать и напечатать сводку

Путь к файлу — ACL_BOARD_PATH, иначе стандартное место в хранилище Obsidian.
"""
from __future__ import annotations

import argparse
import html
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterable

BOARD_ENV = "ACL_BOARD_PATH"
OUT_ENV = "ACL_BOARD_HTML"

DEFAULT_BOARD = (
    Path.home() / "Desktop" / "Obsidian" / "Kuwait Forms" / "дешборд" / "ACL — Task Board.md"
)

#: статусы в порядке от сделанного к незачатому — порядок задаёт вид доски
STATUSES = ("shipped", "review", "next", "blocked", "later", "idea")
DEFAULT_STATUS = "idea"

STATUS_LABEL = {
    "shipped": "Shipped",
    "review": "In review",
    "next": "Next",
    "blocked": "Blocked",
    "later": "Later",
    "idea": "Idea",
}

TRACK_LABEL = {
    "dashboard": "Dashboard",
    "rpa": "RPA",
    "analysis": "Analysis",
    "content": "Content",
    "ops": "Ops",
}

ID_RE = re.compile(r"`(acl-\d+)`")
TASK_RE = re.compile(r"^-\s*\[( |x|X)\]\s*(.+)$")
AREA_RE = re.compile(r"^##\s+(\d+)\s*[·.·-]\s*(.+)$")
CAP_RE = re.compile(r"^###\s+(.+)$")
BLURB_RE = re.compile(r"^>\s*(.+)$")
TAG_RE = re.compile(r"(?<!\w)#([a-z][a-z0-9-]*)")
TRACK_RE = re.compile(r"(?<!\w)@([a-z][a-z0-9-]*)")


@dataclass
class Task:
    id: str
    title: str
    status: str = DEFAULT_STATUS
    track: str = ""
    note: str = ""
    done: bool = False
    line: int = 0

    @property
    def is_done(self) -> bool:
        return self.done or self.status == "shipped"


@dataclass
class Capability:
    title: str
    tasks: list[Task] = field(default_factory=list)


@dataclass
class Area:
    number: str
    title: str
    blurb: str = ""
    caps: list[Capability] = field(default_factory=list)

    @property
    def tasks(self) -> list[Task]:
        return [t for c in self.caps for t in c.tasks]

    @property
    def progress(self) -> tuple[int, int]:
        tasks = self.tasks
        return sum(1 for t in tasks if t.is_done), len(tasks)


@dataclass
class Board:
    title: str = "ACL"
    updated: str = ""
    areas: list[Area] = field(default_factory=list)

    @property
    def tasks(self) -> list[Task]:
        return [t for a in self.areas for t in a.tasks]

    def by_id(self, task_id: str) -> Task | None:
        for t in self.tasks:
            if t.id == task_id:
                return t
        return None

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in STATUSES}
        for t in self.tasks:
            out[t.status] = out.get(t.status, 0) + 1
        return out


# ─────────────────────────── разбор ───────────────────────────


def board_path() -> Path:
    raw = os.environ.get(BOARD_ENV)
    return Path(raw) if raw else DEFAULT_BOARD


def _strip_markers(text: str) -> tuple[str, str, str]:
    """Выдрать из строки статус и трек, вернуть остаток как заголовок."""
    status = ""
    track = ""
    for tag in TAG_RE.findall(text):
        if tag in STATUSES:
            status = tag
    for t in TRACK_RE.findall(text):
        if t in TRACK_LABEL:
            track = t
    title = TAG_RE.sub("", text)
    title = TRACK_RE.sub("", title)
    title = re.sub(r"\s{2,}", " ", title).strip(" ·—-")
    return title.strip(), status, track


def parse(text: str) -> Board:
    board = Board()
    area: Area | None = None
    cap: Capability | None = None
    task: Task | None = None
    note_lines: list[str] = []
    in_front = False
    in_fence = False

    def flush_note() -> None:
        nonlocal note_lines
        if task is not None and note_lines:
            task.note = " ".join(l.strip() for l in note_lines).strip()
        note_lines = []

    for n, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()

        if n == 1 and line.strip() == "---":
            in_front = True
            continue
        if in_front:
            if line.strip() == "---":
                in_front = False
            elif line.startswith("updated:"):
                board.updated = line.split(":", 1)[1].strip()
            elif line.startswith("board:"):
                board.title = line.split(":", 1)[1].strip()
            continue

        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        m = AREA_RE.match(line)
        if m:
            flush_note()
            task = None
            area = Area(number=m.group(1), title=m.group(2).strip())
            board.areas.append(area)
            cap = None
            continue

        m = CAP_RE.match(line)
        if m and area is not None:
            flush_note()
            task = None
            cap = Capability(title=m.group(1).strip())
            area.caps.append(cap)
            continue

        m = BLURB_RE.match(line)
        if m and area is not None and not area.caps:
            area.blurb = m.group(1).strip()
            continue

        m = TASK_RE.match(line)
        if m and area is not None:
            flush_note()
            body = m.group(2)
            ids = ID_RE.findall(body)
            if not ids:
                task = None
                continue
            body = ID_RE.sub("", body, count=1)
            title, status, track = _strip_markers(body)
            done = m.group(1).lower() == "x"
            if not status:
                status = "shipped" if done else DEFAULT_STATUS
            if done:
                status = "shipped"
            if cap is None:
                cap = Capability(title="")
                area.caps.append(cap)
            task = Task(
                id=ids[0], title=title, status=status, track=track, done=done, line=n
            )
            cap.tasks.append(task)
            continue

        if task is not None and raw.startswith((" ", "\t")) and line.strip():
            note_lines.append(line)
            continue

        if not line.strip():
            flush_note()
            task = None

    flush_note()
    return board


# ─────────────────────────── коммиты ───────────────────────────


def ids_in_commit(message: str) -> list[str]:
    """Идентификаторы задач, упомянутые в сообщении коммита."""
    found = re.findall(r"(?<![\w-])(acl-\d+)(?![\w-])", message, flags=re.IGNORECASE)
    seen: list[str] = []
    for f in found:
        f = f.lower()
        if f not in seen:
            seen.append(f)
    return seen


def head_commit(repo: Path, rev: str = "HEAD") -> tuple[str, str]:
    out = subprocess.run(
        ["git", "-C", str(repo), "show", "-s", "--format=%h%n%B", rev],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "git show не отработал")
    sha, _, message = out.stdout.partition("\n")
    return sha.strip(), message.strip()


def apply_commit(text: str, sha: str, message: str, when: str = "") -> tuple[str, list[str]]:
    """Отметить задачи, названные в коммите, и записать строку в журнал."""
    board = parse(text)
    ids = [i for i in ids_in_commit(message) if board.by_id(i)]
    if not ids:
        return text, []

    when = when or date.today().isoformat()
    lines = text.splitlines()
    ticked: list[str] = []

    for task_id in ids:
        task = board.by_id(task_id)
        if task is None or task.is_done:
            continue
        idx = task.line - 1
        line = lines[idx]
        line = line.replace("- [ ]", "- [x]", 1)
        line = TAG_RE.sub(
            lambda m: "#shipped" if m.group(1) in STATUSES else m.group(0), line
        )
        if "#shipped" not in line:
            line = line.rstrip() + " #shipped"
        lines[idx] = line
        ticked.append(task_id)

    if not ticked:
        return text, []

    subject = message.splitlines()[0].strip()
    entry = f"- {when} · `{sha}` · " + ", ".join(f"`{i}`" for i in ticked) + f" — {subject}"
    try:
        at = next(i for i, l in enumerate(lines) if l.strip() == "## Log")
    except StopIteration:
        lines += ["", "## Log", ""]
        at = len(lines) - 2
    # новая запись — первой в списке; между заголовком и списком пустая строка
    insert = at + 1
    if insert < len(lines) and not lines[insert].strip():
        insert += 1
    else:
        lines.insert(insert, "")
        insert += 1
    lines.insert(insert, entry)

    out = "\n".join(lines)
    if not out.endswith("\n"):
        out += "\n"
    return _bump_updated(out, when), ticked


def _bump_updated(text: str, when: str) -> str:
    return re.sub(r"^updated:.*$", f"updated: {when}", text, count=1, flags=re.MULTILINE)


# ─────────────────────────── витрина ───────────────────────────


def _esc(s: str) -> str:
    return html.escape(s, quote=True)


def _rich(s: str) -> str:
    """Экранировать и превратить `код` в <code> — имена файлов и id читаются."""
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", _esc(s))


def _card(task: Task, tilt: int) -> str:
    note = (
        f'<p class="note">{_rich(task.note)}</p>' if task.note else ""
    )
    track = (
        f'<span class="track">{_esc(TRACK_LABEL.get(task.track, task.track))}</span>'
        if task.track
        else ""
    )
    tick = '<span class="tick" aria-hidden="true">✓</span>' if task.is_done else ""
    return (
        f'<article class="pin s-{task.status}" style="--tilt:{tilt / 10:.1f}deg" '
        f'data-status="{task.status}" data-track="{_esc(task.track)}">'
        f'<header><code>{_esc(task.id)}</code>{track}</header>'
        f'<h4>{tick}{_rich(task.title)}</h4>{note}'
        f'<footer>{_esc(STATUS_LABEL.get(task.status, task.status))}</footer>'
        "</article>"
    )


def _tilts() -> Iterable[int]:
    """Наклон стикеров: повторяемый, чтобы доска не дёргалась между сборками."""
    pattern = (-7, 5, -3, 8, -5, 3, 6, -8, 4, -4)
    i = 0
    while True:
        yield pattern[i % len(pattern)]
        i += 1


def render(board: Board) -> str:
    tilt = _tilts()
    counts = board.counts()
    total = len(board.tasks)
    shipped = sum(1 for t in board.tasks if t.is_done)

    legend = "".join(
        f'<button class="chip s-{s}" data-filter="{s}">{STATUS_LABEL[s]}'
        f'<b>{counts.get(s, 0)}</b></button>'
        for s in STATUSES
        if counts.get(s)
    )

    areas_html = []
    for area in board.areas:
        done, tot = area.progress
        pct = round(done / tot * 100) if tot else 0
        caps = []
        for cap in area.caps:
            if not cap.tasks:
                continue
            cards = "".join(_card(t, next(tilt)) for t in cap.tasks)
            caps.append(
                f'<section class="cap"><h3>{_esc(cap.title)}</h3>'
                f'<div class="pins">{cards}</div></section>'
            )
        blurb = f'<p class="blurb">{_esc(area.blurb)}</p>' if area.blurb else ""
        areas_html.append(
            f'<section class="area" id="area-{area.number}">'
            f'<div class="area-head">'
            f'<div class="area-id">{_esc(area.number)}</div>'
            f'<div class="area-name"><h2>{_esc(area.title)}</h2>{blurb}</div>'
            f'<div class="area-meter"><div class="bar"><span style="width:{pct}%"></span></div>'
            f'<div class="ratio">{done}<i>/{tot}</i></div></div>'
            f"</div>"
            f'<div class="caps">{"".join(caps)}</div>'
            f"</section>"
        )

    nav = "".join(
        f'<a href="#area-{a.number}"><b>{_esc(a.number)}</b>{_esc(a.title)}</a>'
        for a in board.areas
    )

    return _PAGE.format(
        title=_esc(board.title),
        updated=_esc(board.updated or date.today().isoformat()),
        shipped=shipped,
        total=total,
        pct=round(shipped / total * 100) if total else 0,
        legend=legend,
        nav=nav,
        areas="".join(areas_html),
    )


_PAGE = """<title>ACL Task Wall</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Familjen+Grotesk:wght@500;600;700&family=Public+Sans:wght@400;500;600&family=JetBrains+Mono:wght@500&display=swap">
<style>
:root {{
  --ground: #eceef0;
  --panel: #f7f8f9;
  --ink: #16191c;
  --ink-2: #454d54;
  --ink-3: #79828a;
  --rule: #d3d8dc;
  --accent: #23456b;
  --shadow: 0 1px 2px rgba(20,26,32,.16), 0 6px 14px rgba(20,26,32,.09);
  --pin-shipped-bg: #dfe9dd; --pin-shipped-ink: #2f5233; --pin-shipped-edge: #b8ccb6;
  --pin-review-bg:  #f6e7c8; --pin-review-ink:  #74521a; --pin-review-edge: #e0c795;
  --pin-next-bg:    #dbe4f0; --pin-next-ink:    #23456b; --pin-next-edge:  #b3c5dd;
  --pin-blocked-bg: #f3d9d5; --pin-blocked-ink: #8b332a; --pin-blocked-edge:#e0b6af;
  --pin-later-bg:   #e6e8ea; --pin-later-ink:   #4d555c; --pin-later-edge: #ccd1d5;
  --pin-idea-bg:    #f7f8f9; --pin-idea-ink:    #6b747c; --pin-idea-edge:  #cfd5da;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --ground: #14171a; --panel: #1c2024; --ink: #eef1f3; --ink-2: #b4bcc3;
    --ink-3: #7d868e; --rule: #2e343a; --accent: #9db8db;
    --shadow: 0 1px 2px rgba(0,0,0,.5), 0 8px 18px rgba(0,0,0,.38);
    --pin-shipped-bg:#25352a; --pin-shipped-ink:#b3d3b5; --pin-shipped-edge:#37503d;
    --pin-review-bg: #3a301b; --pin-review-ink: #e2c286; --pin-review-edge: #574526;
    --pin-next-bg:   #1f2c3d; --pin-next-ink:   #a8c4e6; --pin-next-edge:  #2f4460;
    --pin-blocked-bg:#3b2422; --pin-blocked-ink:#e0a89f; --pin-blocked-edge:#59332e;
    --pin-later-bg:  #23282c; --pin-later-ink:  #98a1a9; --pin-later-edge: #333a40;
    --pin-idea-bg:   #1b1f23; --pin-idea-ink:   #838c94; --pin-idea-edge:  #2c3238;
  }}
}}
:root[data-theme="dark"] {{
  --ground: #14171a; --panel: #1c2024; --ink: #eef1f3; --ink-2: #b4bcc3;
  --ink-3: #7d868e; --rule: #2e343a; --accent: #9db8db;
  --shadow: 0 1px 2px rgba(0,0,0,.5), 0 8px 18px rgba(0,0,0,.38);
  --pin-shipped-bg:#25352a; --pin-shipped-ink:#b3d3b5; --pin-shipped-edge:#37503d;
  --pin-review-bg: #3a301b; --pin-review-ink: #e2c286; --pin-review-edge: #574526;
  --pin-next-bg:   #1f2c3d; --pin-next-ink:   #a8c4e6; --pin-next-edge:  #2f4460;
  --pin-blocked-bg:#3b2422; --pin-blocked-ink:#e0a89f; --pin-blocked-edge:#59332e;
  --pin-later-bg:  #23282c; --pin-later-ink:  #98a1a9; --pin-later-edge: #333a40;
  --pin-idea-bg:   #1b1f23; --pin-idea-ink:   #838c94; --pin-idea-edge:  #2c3238;
}}

* {{ box-sizing: border-box; }}
body {{
  margin: 0; background: var(--ground); color: var(--ink);
  font-family: "Public Sans", ui-sans-serif, system-ui, sans-serif;
  font-size: 15px; line-height: 1.5;
}}
.wrap {{ max-width: 1180px; margin: 0 auto; padding: 40px 24px 80px; }}

header.top {{ display: flex; flex-wrap: wrap; gap: 24px 40px; align-items: flex-end;
  border-bottom: 2px solid var(--ink); padding-bottom: 18px; }}
header.top h1 {{
  font-family: "Familjen Grotesk", "Public Sans", sans-serif;
  font-weight: 700; font-size: clamp(30px, 5vw, 44px); line-height: 1.05;
  margin: 0; letter-spacing: -.02em; text-wrap: balance;
}}
header.top .sub {{ color: var(--ink-2); margin: 8px 0 0; max-width: 46ch; }}
.score {{ margin-left: auto; text-align: right; }}
.score .big {{ font-family: "Familjen Grotesk", sans-serif; font-size: 40px;
  font-weight: 700; line-height: 1; font-variant-numeric: tabular-nums; }}
.score .big i {{ font-style: normal; color: var(--ink-3); font-size: 22px; }}
.score .cap-label {{ text-transform: uppercase; letter-spacing: .09em; font-size: 10.5px;
  color: var(--ink-3); font-weight: 600; }}

.legend {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 18px 0 0; }}
.chip {{ font: inherit; font-size: 12.5px; font-weight: 600; cursor: pointer;
  border: 1px solid var(--edge, var(--rule)); background: var(--bg, var(--panel));
  color: var(--fg, var(--ink-2)); border-radius: 2px; padding: 5px 9px;
  display: inline-flex; gap: 7px; align-items: center; }}
.chip b {{ font-variant-numeric: tabular-nums; opacity: .7; }}
.chip[aria-pressed="true"] {{ outline: 2px solid var(--ink); outline-offset: 1px; }}
.chip:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}

nav.areas {{ display: flex; flex-wrap: wrap; gap: 0 22px; margin: 26px 0 0;
  border-top: 1px solid var(--rule); padding-top: 12px; }}
nav.areas a {{ color: var(--ink-2); text-decoration: none; font-size: 13px;
  font-weight: 500; display: inline-flex; gap: 7px; padding: 3px 0; }}
nav.areas a b {{ color: var(--ink-3); font-variant-numeric: tabular-nums; }}
nav.areas a:hover {{ color: var(--ink); text-decoration: underline;
  text-underline-offset: 3px; }}

.area {{ margin-top: 52px; scroll-margin-top: 20px; }}
.area-head {{ display: flex; gap: 18px; align-items: flex-start;
  border-bottom: 1px solid var(--rule); padding-bottom: 12px; }}
.area-id {{ font-family: "Familjen Grotesk", sans-serif; font-weight: 700;
  font-size: 30px; line-height: 1; color: var(--ink-3);
  font-variant-numeric: tabular-nums; min-width: 28px; }}
.area-name h2 {{ font-family: "Familjen Grotesk", sans-serif; font-weight: 600;
  font-size: 22px; margin: 0; letter-spacing: -.01em; }}
.area-name .blurb {{ margin: 4px 0 0; color: var(--ink-2); font-size: 13.5px;
  max-width: 62ch; }}
.area-meter {{ margin-left: auto; text-align: right; min-width: 120px; }}
.area-meter .bar {{ height: 5px; background: var(--rule); border-radius: 3px;
  overflow: hidden; }}
.area-meter .bar span {{ display: block; height: 100%; background: var(--accent); }}
.area-meter .ratio {{ font-size: 12px; color: var(--ink-3); margin-top: 5px;
  font-variant-numeric: tabular-nums; font-weight: 600; }}
.area-meter .ratio i {{ font-style: normal; opacity: .65; }}

.caps {{ display: grid; gap: 26px 20px; margin-top: 22px;
  grid-template-columns: repeat(auto-fit, minmax(248px, 1fr)); }}
.cap h3 {{ font-size: 11px; text-transform: uppercase; letter-spacing: .1em;
  font-weight: 600; color: var(--ink-3); margin: 0 0 12px;
  border-bottom: 1px solid var(--rule); padding-bottom: 7px; }}
.pins {{ display: flex; flex-direction: column; gap: 12px; }}

.pin {{ background: var(--bg); color: var(--fg); border: 1px solid var(--edge);
  border-radius: 2px; padding: 11px 12px 9px; box-shadow: var(--shadow);
  transform: rotate(var(--tilt)); transition: transform .14s ease, box-shadow .14s ease; }}
.pin:hover {{ transform: rotate(0deg) translateY(-2px);
  box-shadow: 0 2px 4px rgba(20,26,32,.18), 0 12px 24px rgba(20,26,32,.14); }}
.pin header {{ display: flex; gap: 8px; align-items: center; margin-bottom: 6px; }}
.pin code {{ font-family: "JetBrains Mono", ui-monospace, monospace; font-size: 10px;
  letter-spacing: .02em; opacity: .72; }}
.pin .track {{ margin-left: auto; font-size: 9.5px; font-weight: 600;
  text-transform: uppercase; letter-spacing: .08em; opacity: .72; }}
.pin h4 {{ margin: 0; font-size: 14.5px; font-weight: 600; line-height: 1.3;
  letter-spacing: -.005em; text-wrap: balance; }}
.pin .tick {{ margin-right: 5px; }}
.pin .note {{ margin: 7px 0 0; font-size: 12.5px; line-height: 1.42; opacity: .82; }}
.pin h4 code, .pin .note code {{ font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: .84em; opacity: 1; background: rgba(127,127,127,.16);
  padding: 0 3px; border-radius: 2px; overflow-wrap: anywhere; }}
.pin footer {{ margin-top: 9px; padding-top: 7px; border-top: 1px solid var(--edge);
  font-size: 9.5px; font-weight: 600; text-transform: uppercase;
  letter-spacing: .09em; opacity: .68; }}
.pin.hide {{ display: none; }}

.s-shipped {{ --bg: var(--pin-shipped-bg); --fg: var(--pin-shipped-ink); --edge: var(--pin-shipped-edge); }}
.s-review  {{ --bg: var(--pin-review-bg);  --fg: var(--pin-review-ink);  --edge: var(--pin-review-edge); }}
.s-next    {{ --bg: var(--pin-next-bg);    --fg: var(--pin-next-ink);    --edge: var(--pin-next-edge); }}
.s-blocked {{ --bg: var(--pin-blocked-bg); --fg: var(--pin-blocked-ink); --edge: var(--pin-blocked-edge); }}
.s-later   {{ --bg: var(--pin-later-bg);   --fg: var(--pin-later-ink);   --edge: var(--pin-later-edge); }}
.s-idea    {{ --bg: var(--pin-idea-bg);    --fg: var(--pin-idea-ink);    --edge: var(--pin-idea-edge);
  border-style: dashed; box-shadow: none; }}

footer.foot {{ margin-top: 64px; border-top: 1px solid var(--rule); padding-top: 14px;
  color: var(--ink-3); font-size: 12.5px; display: flex; flex-wrap: wrap; gap: 6px 20px; }}
footer.foot code {{ font-family: "JetBrains Mono", monospace; font-size: 11.5px; }}
@media (prefers-reduced-motion: reduce) {{ .pin {{ transition: none; }} }}
@media print {{ .legend, nav.areas {{ display: none; }} .pin {{ transform: none; box-shadow: none; }} }}
</style>

<div class="wrap">
  <header class="top">
    <div>
      <h1>ACL Task Wall</h1>
      <p class="sub">Five areas of the system, the capabilities each is made of,
      and every task hanging off them. Edited in Obsidian; this wall is built
      from that file.</p>
    </div>
    <div class="score">
      <div class="cap-label">Shipped</div>
      <div class="big">{shipped}<i>/{total}</i></div>
      <div class="cap-label">{pct}% of the wall</div>
    </div>
  </header>

  <div class="legend" role="group" aria-label="Filter by status">{legend}</div>
  <nav class="areas">{nav}</nav>

  {areas}

  <footer class="foot">
    <span>Source of truth: <code>ACL — Task Board.md</code> in Obsidian</span>
    <span>Last change: {updated}</span>
    <span>Built by <code>scripts/acl_board.py</code></span>
  </footer>
</div>

<script>
(function () {{
  var active = null;
  var chips = Array.prototype.slice.call(document.querySelectorAll('.chip'));
  chips.forEach(function (chip) {{
    chip.setAttribute('aria-pressed', 'false');
    chip.addEventListener('click', function () {{
      var want = chip.dataset.filter;
      active = (active === want) ? null : want;
      chips.forEach(function (c) {{
        c.setAttribute('aria-pressed', String(c.dataset.filter === active));
      }});
      document.querySelectorAll('.pin').forEach(function (pin) {{
        pin.classList.toggle('hide', !!active && pin.dataset.status !== active);
      }});
      document.querySelectorAll('.cap').forEach(function (cap) {{
        var any = cap.querySelector('.pin:not(.hide)');
        cap.style.display = any ? '' : 'none';
      }});
    }});
  }});
}})();
</script>
"""


# ─────────────────────────── команды ───────────────────────────


def default_out(repo: Path) -> Path:
    raw = os.environ.get(OUT_ENV)
    return Path(raw) if raw else repo / "output" / "acl_task_wall.html"


def cmd_render(args: argparse.Namespace, repo: Path) -> int:
    src = board_path()
    if not src.exists():
        print(f"нет файла доски: {src}", file=sys.stderr)
        return 2
    board = parse(src.read_text(encoding="utf-8"))
    out = Path(args.out) if args.out else default_out(repo)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(board), encoding="utf-8")
    done = sum(1 for t in board.tasks if t.is_done)
    print(f"доска собрана: {out}")
    print(f"  {len(board.areas)} областей, {len(board.tasks)} задач, {done} сделано")
    return 0


def cmd_check(args: argparse.Namespace, repo: Path) -> int:
    src = board_path()
    if not src.exists():
        print(f"нет файла доски: {src}", file=sys.stderr)
        return 2
    board = parse(src.read_text(encoding="utf-8"))
    ids = [t.id for t in board.tasks]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    for area in board.areas:
        done, tot = area.progress
        print(f"{area.number}. {area.title}  —  {done}/{tot}")
        for cap in area.caps:
            for t in cap.tasks:
                mark = "x" if t.is_done else " "
                print(f"   [{mark}] {t.id:<9} {t.status:<8} {t.title}")
    if dupes:
        print("\nповторяющиеся id: " + ", ".join(dupes), file=sys.stderr)
        return 1
    return 0


def cmd_sync(args: argparse.Namespace, repo: Path) -> int:
    src = board_path()
    if not src.exists():
        # хук не должен ломать коммит из-за отсутствующего хранилища
        print(f"доска не найдена ({src}) — пропускаю")
        return 0
    try:
        sha, message = head_commit(repo, args.rev)
    except RuntimeError as exc:
        print(f"не смог прочитать коммит: {exc}", file=sys.stderr)
        return 0
    text = src.read_text(encoding="utf-8")
    new, ticked = apply_commit(text, sha, message)
    if ticked:
        src.write_text(new, encoding="utf-8")
        print("отмечено: " + ", ".join(ticked))
    else:
        print("в коммите нет id задач — доска не тронута")
    board = parse(new)
    out = default_out(repo)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(board), encoding="utf-8")
    return 0


def cmd_install_hook(args: argparse.Namespace, repo: Path) -> int:
    """Положить post-commit в общий каталог хуков — он один на все worktree."""
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        print("это не репозиторий git", file=sys.stderr)
        return 2
    common = Path(out.stdout.strip())
    if not common.is_absolute():
        common = repo / common
    hooks = common / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    dst = hooks / "post-commit"
    src = repo / "scripts" / "hooks" / "post-commit"
    if dst.exists() and "acl_board.py" not in dst.read_text(encoding="utf-8", errors="replace"):
        print(f"уже есть чужой хук: {dst} — впишите вызов вручную", file=sys.stderr)
        return 1
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    dst.chmod(0o755)
    print(f"хук поставлен: {dst}")
    print("  теперь id задачи в сообщении коммита отмечает её на доске")
    return 0


def main(argv: list[str] | None = None) -> int:
    # консоль Windows по умолчанию cp1252 — без этого падает на первом же слове
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    repo = Path(__file__).resolve().parent.parent
    p = argparse.ArgumentParser(description="доска задач ACL")
    p.add_argument("--render", action="store_true", help="собрать HTML")
    p.add_argument("--sync", action="store_true", help="отметить задачи из коммита")
    p.add_argument("--check", action="store_true", help="разобрать и напечатать")
    p.add_argument("--install-hook", action="store_true", help="поставить post-commit")
    p.add_argument("--rev", default="HEAD", help="какой коммит читать при --sync")
    p.add_argument("--out", default="", help="куда положить HTML")
    args = p.parse_args(argv)

    if args.install_hook:
        return cmd_install_hook(args, repo)
    if args.sync:
        return cmd_sync(args, repo)
    if args.check:
        return cmd_check(args, repo)
    return cmd_render(args, repo)


if __name__ == "__main__":
    raise SystemExit(main())
