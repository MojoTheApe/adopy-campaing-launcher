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
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import quote
from typing import Iterable

BOARD_ENV = "ACL_BOARD_PATH"
OUT_ENV = "ACL_BOARD_HTML"

BOARD_NAME = "ACL — Task Board.md"

#: статусы в порядке от сделанного к незачатому — порядок задаёт вид доски
#: «Влито» и «роздано» — разные вещи, и между ними бывает много дней.
#: Автообновление принимает только вершину master с аннотированным
#: `acl-release-*` тегом: без него код в master есть, а на машинах его нет.
#: Пока состояния было два, слитое приходилось звать `shipped` — и доска
#: утверждала, что работа у людей, когда она лежала только в репозитории.
STATUSES = ("shipped", "merged", "closed", "review", "next", "blocked", "later", "idea")
DEFAULT_STATUS = "idea"

STATUS_LABEL = {
    "shipped": "Shipped",
    "merged": "In master",
    "closed": "Closed",
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

ID_RE = re.compile(r"`(acl-\d+[a-z]?)`")
TASK_RE = re.compile(r"^-\s*\[( |x|X)\]\s*(.+)$")
SUB_RE = re.compile(r"^\s+-\s*\[( |x|X)\]\s*(.+)$")
AREA_RE = re.compile(r"^##\s+(\d+)\s*[·.·-]\s*(.+)$")
CAP_RE = re.compile(r"^###\s+(.+)$")
BLURB_RE = re.compile(r"^>\s*(.+)$")
TAG_RE = re.compile(r"(?<!\w)#([a-z][a-z0-9-]*)")
TRACK_RE = re.compile(r"(?<!\w)@([a-z][a-z0-9-]*)")
NEEDS_RE = re.compile(r"needs:(acl-\d+[a-z]?)")


@dataclass
class Task:
    id: str
    title: str
    status: str = DEFAULT_STATUS
    track: str = ""
    note: str = ""
    done: bool = False
    line: int = 0
    subs: list["Task"] = field(default_factory=list)
    needs: list[str] = field(default_factory=list)
    explicit: bool = False  # статус написан рукой, а не выведен из галочки
    source_url: str = ""
    source_label: str = ""

    @property
    def is_done(self) -> bool:
        """Работа закончена. Это не то же самое, что «доехало до людей»."""
        if self.done or self.status in {"merged", "shipped"}:
            return True
        # задача, у которой сделаны все части, сделана — это факт, не оценка
        return bool(self.subs) and all(s.is_done for s in self.subs)

    @property
    def shown_status(self) -> str:
        """Что показывать на доске.

        Галочка и статус — про разное: галочка про то, доделана ли работа,
        статус — про то, где она лежит. `- [x] … #review` значит «готово, но
        живёт в ветке и ещё не влито», и раньше галочка молча переписывала это
        в shipped: задача выглядела доехавшей до людей, ничего им не доехав.
        """
        if self.explicit:
            return self.status
        return "shipped" if self.is_done else self.status

    @property
    def sub_progress(self) -> tuple[int, int]:
        return sum(1 for s in self.subs if s.is_done), len(self.subs)


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
    description: str = ""
    source_url: str = ""
    source_label: str = ""

    @property
    def tasks(self) -> list[Task]:
        """Только верхний уровень — по нему считается прогресс областей."""
        return [t for a in self.areas for t in a.tasks]

    @property
    def all_tasks(self) -> list[Task]:
        """Вместе с подзадачами — по ним ищут id и отмечают коммитом."""
        out: list[Task] = []
        for t in self.tasks:
            out.append(t)
            out.extend(t.subs)
        return out

    def by_id(self, task_id: str) -> Task | None:
        for t in self.all_tasks:
            if t.id == task_id:
                return t
        return None

    def parent_of(self, task_id: str) -> Task | None:
        for t in self.tasks:
            if any(s.id == task_id for s in t.subs):
                return t
        return None

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in STATUSES}
        for t in self.tasks:
            out[t.shown_status] = out.get(t.shown_status, 0) + 1
        return out


# ─────────────────────────── разбор ───────────────────────────


def board_path() -> Path:
    raw = os.environ.get(BOARD_ENV)
    if raw:
        return Path(raw)
    root = str(Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    from config import paths

    vault = paths.get_obsidian_dashboard_dir()
    if vault is None:
        print(f"[доска] {paths.OBSIDIAN_MISSING_WARNING}", file=sys.stderr)
        vault = Path.home() / "Desktop" / "Obsidian" / "дешборд"
    return vault / BOARD_NAME


def _strip_markers(text: str) -> tuple[str, str, str, list[str]]:
    """Выдрать статус, трек и зависимости; остаток вернуть как заголовок."""
    status = ""
    track = ""
    for tag in TAG_RE.findall(text):
        if tag in STATUSES:
            status = tag
    for t in TRACK_RE.findall(text):
        if t in TRACK_LABEL:
            track = t
    needs: list[str] = []
    for dep in NEEDS_RE.findall(text):
        if dep not in needs:
            needs.append(dep)
    title = NEEDS_RE.sub("", text)
    title = TAG_RE.sub("", title)
    title = TRACK_RE.sub("", title)
    title = re.sub(r"\s{2,}", " ", title).strip(" ·—-")
    return title.strip(), status, track, needs


def parse(text: str) -> Board:
    board = Board()
    area: Area | None = None
    cap: Capability | None = None
    task: Task | None = None
    note_at: Task | None = None  # примечание цепляется к последней строке — задаче или подзадаче
    note_lines: list[str] = []
    in_front = False
    in_fence = False

    def flush_note() -> None:
        nonlocal note_lines
        if note_at is not None and note_lines:
            note_at.note = " ".join(l.strip() for l in note_lines).strip()
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
            task = note_at = None
            area = Area(number=m.group(1), title=m.group(2).strip())
            board.areas.append(area)
            cap = None
            continue

        m = CAP_RE.match(line)
        if m and area is not None:
            flush_note()
            task = note_at = None
            cap = Capability(title=m.group(1).strip())
            area.caps.append(cap)
            continue

        m = BLURB_RE.match(line)
        if m and area is not None and not area.caps:
            area.blurb = m.group(1).strip()
            continue

        m = SUB_RE.match(line)
        if m and task is not None:
            flush_note()
            body = m.group(2)
            ids = ID_RE.findall(body)
            body = ID_RE.sub("", body, count=1) if ids else body
            title, status, track, needs = _strip_markers(body)
            done = m.group(1).lower() == "x"
            task.subs.append(
                Task(
                    id=ids[0] if ids else "",
                    title=title,
                    status=status or ("shipped" if done else DEFAULT_STATUS),
                    track=track or task.track,
                    done=done,
                    line=n,
                    needs=needs,
                    explicit=bool(status),
                )
            )
            note_at = task.subs[-1]
            continue

        m = TASK_RE.match(line)
        if m and area is not None:
            flush_note()
            body = m.group(2)
            ids = ID_RE.findall(body)
            if not ids:
                task = note_at = None
                continue
            body = ID_RE.sub("", body, count=1)
            title, status, track, needs = _strip_markers(body)
            done = m.group(1).lower() == "x"
            explicit = bool(status)
            if not status:
                status = "shipped" if done else DEFAULT_STATUS
            if cap is None:
                cap = Capability(title="")
                area.caps.append(cap)
            task = Task(
                id=ids[0],
                title=title,
                status=status,
                track=track,
                done=done,
                line=n,
                needs=needs,
                explicit=explicit,
            )
            cap.tasks.append(task)
            note_at = task
            continue

        if task is not None and raw.startswith((" ", "\t")) and line.strip():
            note_lines.append(line)
            continue

        if not line.strip():
            flush_note()
            task = note_at = None

    flush_note()
    return board


# ─────────────────────────── коммиты ───────────────────────────


#: id со всеми границами — «acl-103xy» и «acl-103-beta» сюда не попадают
_ID_IN_TEXT = re.compile(r"(?<![\w-])(acl-\d+[a-z]?)(?![\w-])", flags=re.IGNORECASE)

#: «closes acl-103», «Board: acl-104, acl-105», «сделано acl-111 и acl-112»
_CLOSER_BEFORE = re.compile(
    r"(?<![\w-])(?:closes?d?|fix(?:e[sd])?|done|board|закрыв\w*|закрыт\w*|сделан\w*)"
    r"\s*:?\s*(?:acl-\d+[a-z]?[\s,]*(?:и\s+)?)*$",
    flags=re.IGNORECASE,
)
#: «acl-103 закрыто», «acl-104b готово»
_CLOSER_AFTER = re.compile(
    r"^(?:[\s,]*(?:и\s+)?acl-\d+[a-z]?)*[\s,]*"
    r"(?:closed|done|fixed|закрыт\w*|сделан\w*|готов\w*)(?![\w-])",
    flags=re.IGNORECASE,
)


def ids_in_commit(message: str) -> list[str]:
    """Задачи, которые коммит объявляет закрытыми.

    Одного упоминания id мало — рядом должно стоять закрывающее слово. Иначе
    любой текст, где задача просто названа (пример в документации, объяснение
    зависимости), молча ставил бы галочку. Так и случилось при первом же
    применении: коммит со строкой «needs:acl-105» закрыл acl-105.
    """
    seen: list[str] = []
    for m in _ID_IN_TEXT.finditer(message):
        if not (
            _CLOSER_BEFORE.search(message[: m.start()])
            or _CLOSER_AFTER.match(message[m.end() :])
        ):
            continue
        low = m.group(1).lower()
        if low not in seen:
            seen.append(low)
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


def branch_of(repo: Path) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    )
    return out.stdout.strip() if out.returncode == 0 else ""


MAIN_BRANCHES = {"master", "main"}


def apply_commit(
    text: str, sha: str, message: str, when: str = "", branch: str = "master"
) -> tuple[str, list[str]]:
    """Отметить задачи, названные в коммите, и записать строку в журнал.

    Feature-коммит означает review, основной коммит — merged. Только
    отдельное подтверждение выпуска позволяет пометить задачу shipped.
    """
    board = parse(text)
    ambiguous = set(duplicate_ids(board))
    ids = [i for i in ids_in_commit(message) if i not in ambiguous and board.by_id(i)]
    if not ids:
        return text, []

    when = when or date.today().isoformat()
    landed = "merged" if branch in MAIN_BRANCHES else "review"
    completed_rank = {"review": 0, "merged": 1, "shipped": 2}
    lines = text.splitlines()
    ticked: list[str] = []

    def tick(task: Task, add_tag: bool, status: str = landed) -> None:
        idx = task.line - 1
        line = lines[idx].replace("- [ ]", "- [x]", 1)
        line = TAG_RE.sub(
            lambda m: "#" + status if m.group(1) in STATUSES else m.group(0), line
        )
        if add_tag and "#" + status not in line:
            line = line.rstrip() + " #" + status
        lines[idx] = line
        task.done = True
        task.status = status
        task.explicit = True

    for task_id in ids:
        task = board.by_id(task_id)
        if task is None or (task.is_done and
                completed_rank.get(task.shown_status, -1) >= completed_rank[landed]):
            continue
        # У подзадач статус обычно не пишем: `shipped` читается по галочке, а
        # строка и так узкая. Но `review` галочка не несёт — «сделано» и
        # «доехало» это разные вещи, — и без тега подзадача в ветке выглядела
        # бы розданной. Так и вышло с acl-108a.
        parent = board.parent_of(task_id)
        tick(task, add_tag=parent is None or landed != "shipped")
        ticked.append(task_id)

        # закрылась последняя часть — родитель закрыт вместе с ней
        if (parent is not None and parent.id not in ambiguous
                and parent.is_done and parent.shown_status != "shipped"):
            child_states = {s.shown_status for s in parent.subs}
            parent_status = ("shipped" if child_states == {"shipped"} else
                             "merged" if child_states <= {"merged", "shipped"} else "review")
            if completed_rank.get(parent.shown_status, -1) >= completed_rank[parent_status]:
                continue
            tick(parent, add_tag=True, status=parent_status)
            # и в журнал: задача закончилась этим коммитом, а без записи
            # карточка потом не знает, на какой ветке её искать
            if parent.id not in ticked:
                ticked.append(parent.id)

    if not ticked:
        return text, []

    subject = message.splitlines()[0].strip()
    where = "" if branch in MAIN_BRANCHES else f" · `{branch}`"
    entry = (
        f"- {when} · `{sha}`" + where + " · "
        + ", ".join(f"`{i}`" for i in ticked)
        + f" — {subject}"
    )
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


#: Где искать ветку на GitHub. Рабочий репозиторий, тот, куда получают доступ
#: разработчики; переопределяется переменной окружения.
REPO_URL = os.environ.get(
    "ACL_REPO_URL", "https://github.com/MojoTheApe/adopy-campaing-launcher"
).rstrip("/")

#: строка журнала: «- 2026-09-08 · `e85e4ee` · `feature/…` · `acl-703b` — …»
_LOG_LINE = re.compile(
    r"^-\s*(\d{4}-\d{2}-\d{2})\s*·\s*`([0-9a-f]{6,40})`"
    r"(?:\s*·\s*`([^`]+)`)?\s*·\s*(.+?)\s+—",
    re.MULTILINE,
)


def landings(text: str) -> dict[str, dict[str, str]]:
    """Где лежит работа по каждой задаче: коммит, ветка, день.

    Берётся из журнала, который хук и так ведёт, а не из нового поля рядом с
    задачей. Второе хранилище того же факта однажды разойдётся с первым, и
    заметит это тот, кто поверит не тому.

    Побеждает верхняя запись: журнал ведётся сверху вниз, новыми вперёд.
    """
    out: dict[str, dict[str, str]] = {}
    for when, sha, branch, ids in _LOG_LINE.findall(text):
        for task_id in re.findall(r"`(acl-\d+[a-z]?)`", ids):
            out.setdefault(task_id.lower(), {"sha": sha, "branch": branch or "", "when": when})
    return out


# ─────────────────────────── витрина ───────────────────────────


def _esc(s: str) -> str:
    return html.escape(s, quote=True)


def _rich(s: str) -> str:
    """Экранировать и превратить `код` в <code> — имена файлов и id читаются."""
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", _esc(s))


def _subs(task: Task) -> str:
    if not task.subs:
        return ""
    rows = []
    for s in task.subs:
        box = "☑" if s.is_done else "☐"
        sid = f"<code>{_esc(s.id)}</code>" if s.id else ""
        note = f'<span class="sub-note">{_rich(s.note)}</span>' if s.note else ""
        needs = _needs(s)
        rows.append(
            f'<li class="{"done" if s.is_done else "open"}">'
            f'<span class="box" aria-hidden="true">{box}</span>'
            f'<span class="sub-body">{sid}{_rich(s.title)}{note}{needs}</span></li>'
        )
    return f'<ul class="subs">{"".join(rows)}</ul>'


def _needs(task: Task) -> str:
    if not task.needs:
        return ""
    deps = " ".join(f"<code>{_esc(d)}</code>" for d in task.needs)
    return f'<span class="needs">waits on {deps}</span>'


def _where(task: Task, land: dict) -> str:
    """Где искать работу задачи, ждущей ревью.

    Ставится рядом со статусом, потому что читают их вместе: «в ревью» без
    ответа на вопрос «где» — это состояние, с которым нечего делать.

    Если записи нет, так и написано. Задача, помеченная в ревью рукой, а не
    коммитом, ветки за собой не оставляет — и молчание здесь выглядело бы как
    «ветки нет», а не как «мы её не записали».
    """
    if task.shown_status != "review":
        return ""
    at = land.get(task.id.lower())
    if not at and task.subs:
        # Родителя хук раньше закрывал молча, не называя в журнале. Ветку
        # берём у частей — но только если они согласны между собой: разные
        # ветки у частей это не «примерно там», а вопрос, на который у нас
        # нет ответа.
        seen = {
            land[s.id.lower()]["branch"]
            for s in task.subs
            if s.id.lower() in land and land[s.id.lower()].get("branch")
        }
        if len(seen) == 1 and all(land.get(s.id.lower(), {}).get("branch") for s in task.subs):
            branch_only = seen.pop()
            at = {"branch": branch_only, "sha": ""}
    if not at:
        return '<span class="where none" title="Помечено вручную, а не коммитом — ветка не записана">branch not recorded</span>'
    branch, sha = at.get("branch", ""), at.get("sha", "")
    if not branch:
        if sha:
            return (f'<a class="where" href="{_esc(REPO_URL)}/commit/{quote(sha, safe="")}" '
                    f'target="_blank" rel="noopener"><code>{_esc(sha)}</code></a>')
        return '<span class="where none">branch not recorded</span>'
    short = branch.split("/", 1)[-1]
    tail = f"<code>{_esc(sha)}</code>" if sha else ""
    where = f" · commit {_esc(sha)}" if sha else ""
    return (
        f'<a class="where" href="{_esc(REPO_URL)}/tree/{quote(branch, safe="/")}" target="_blank" rel="noopener"'
        f' title="{_esc(branch)}{where} — открыть ветку на GitHub">'
        f"{_esc(short)}{tail}</a>"
    )


def _card(task: Task, tilt: int, land: dict | None = None) -> str:
    note = f'<p class="note">{_rich(task.note)}</p>' if task.note else ""
    track = (
        f'<span class="track">{_esc(TRACK_LABEL.get(task.track, task.track))}</span>'
        if task.track
        else ""
    )
    tick = '<span class="tick" aria-hidden="true">✓</span>' if task.is_done else ""
    done, total = task.sub_progress
    meter = f'<span class="count">{done}/{total}</span>' if total else ""
    status = task.shown_status
    subs = _subs(task)
    source = (
        f'<p><a href="{_esc(task.source_url)}" target="_blank" rel="noopener noreferrer">'
        f'{_esc(task.source_label or "Open source")}</a></p>'
        if task.source_url else ""
    )
    body = f"{note}{subs}{source}"
    head = (
        f'<header><code>{_esc(task.id)}</code>{track}</header>'
        f'<h4>{tick}{_rich(task.title)}</h4>'
        f'<footer>{_esc(STATUS_LABEL.get(status, status))}'
        f"{_where(task, land or {})}{_needs(task)}{meter}</footer>"
    )
    attrs = (
        f'class="pin s-{status}" style="--tilt:{tilt / 10:.1f}deg" '
        f'data-status="{status}" data-track="{_esc(task.track)}" '
        f'data-id="{_esc(task.id)}"'
    )
    # Карточка без примечания и подзадач не сворачивается: стрелка, за которой
    # ничего нет, обещает содержимое и не отдаёт его. Свёрнутой карточкой
    # остаётся видно всё, чем задачу опознают, — номер, трек, название, статус
    # и счётчик частей.
    if not body:
        return f"<article {attrs}>{head}</article>"
    return (
        f"<details {attrs}>"
        f'<summary class="pin-head"><span class="pin-caret" aria-hidden="true"></span>'
        f"{head}</summary>"
        f'<div class="pin-body">{body}</div>'
        f"</details>"
    )


def _tilts() -> Iterable[int]:
    """Наклон стикеров: повторяемый, чтобы доска не дёргалась между сборками."""
    pattern = (-7, 5, -3, 8, -5, 3, 6, -8, 4, -4)
    i = 0
    while True:
        yield pattern[i % len(pattern)]
        i += 1


def render(board: Board, land: dict | None = None) -> str:
    land = land or {}
    tilt = _tilts()
    counts = board.counts()
    total = len(board.tasks)
    shipped = sum(1 for t in board.tasks if t.shown_status == "shipped")

    legend = f'<button class="chip" data-filter="all" aria-pressed="true">All<b>{total}</b></button>'
    legend += "".join(
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
            cards = "".join(_card(t, next(tilt), land) for t in cap.tasks)
            caps.append(
                f'<section class="cap"><h3>{_esc(cap.title)}</h3>'
                f'<div class="pins">{cards}</div></section>'
            )
        blurb = f'<p class="blurb">{_esc(area.blurb)}</p>' if area.blurb else ""
        # Область свёрнута: видны заголовок, подзаголовок и счётчик, а карточки
        # открывает стрелка. Шесть областей и шестьдесят с лишним задач разом —
        # это стена, по которой нельзя вести взглядом; свёрнутая, она за один
        # экран отвечает, из чего состоит система и сколько где сделано.
        areas_html.append(
            f'<details class="area" id="area-{area.number}" data-area="{_esc(area.number)}">'
            f'<summary class="area-head">'
            f'<span class="area-caret" aria-hidden="true"></span>'
            f'<div class="area-id">{_esc(area.number)}</div>'
            f'<div class="area-name"><h2>{_esc(area.title)}</h2>{blurb}</div>'
            f'<div class="area-meter"><div class="bar"><span style="width:{pct}%"></span></div>'
            f'<div class="ratio">{done}<i>/{tot}</i></div></div>'
            f"</summary>"
            f'<div class="caps">{"".join(caps)}</div>'
            f"</details>"
        )

    nav = "".join(
        f'<a href="#area-{a.number}"><b>{_esc(a.number)}</b>{_esc(a.title)}</a>'
        for a in board.areas
    )

    return _PAGE.replace("__FAVICON__", _FAVICON).format(
        title=_esc(board.title),
        updated=_esc(board.updated or date.today().isoformat()),
        shipped=shipped,
        total=total,
        pct=round(shipped / total * 100) if total else 0,
        legend=legend,
        nav=nav,
        # Число областей считается, а не написано словом: в подзаголовке
        # стояло «Five areas», когда их было уже шесть. Мелочь, которая учит
        # не верить остальному тексту на странице.
        area_count=len(board.areas),
        areas="".join(areas_html) or '<p class="empty-board" role="status">No tasks yet.</p>',
        description=(_esc(board.description) if board.description else
            f"{len(board.areas)} areas of the system, the capabilities each is made of, "
            "and every task hanging off them. Open an area to see its tasks. Edited in "
            "Obsidian; this wall is built from that file."),
        source=(f'<a href="{_esc(board.source_url)}" target="_blank" rel="noopener noreferrer">'
                f'{_esc(board.source_label)}</a>' if board.source_url else
                '<code>ACL — Task Board.md</code> in Obsidian'),
    )


#: Четыре полоски снизу вверх: заблокировано, следующее, на ревью, роздано.
#: Не украшение — та же лестница состояний, что и на самой доске, поэтому и
#: цвета взяты из её палитры, только насыщеннее: приглушённые на 16 пикселях
#: превращаются в грязь. Подложки нет — вкладка сама решает, тёмная она или
#: светлая, и белый прямоугольник был бы заплаткой на её фоне.
_FAVICON = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E"
    "%3Crect x='2' y='11.4' width='12' height='2.6' rx='1.3' fill='%23b3372c'/%3E"
    "%3Crect x='2' y='7.9' width='12' height='2.6' rx='1.3' fill='%232b5b96'/%3E"
    "%3Crect x='2' y='4.4' width='12' height='2.6' rx='1.3' fill='%23d99b00'/%3E"
    "%3Crect x='2' y='0.9' width='12' height='2.6' rx='1.3' fill='%232f7a3d'/%3E"
    "%3C/svg%3E"
)

_PAGE = """<meta charset="utf-8">
<title>ACL Task Wall</title>
<link rel="icon" type="image/svg+xml" href="__FAVICON__">
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
  --pin-merged-bg:  #dfe9dd; --pin-merged-ink:  #2f5233; --pin-merged-edge: #b8ccb6;
  --pin-closed-bg:  #d9e6f2; --pin-closed-ink:  #1f4a72; --pin-closed-edge: #b0cbe4;
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
    --pin-merged-bg:#25352a; --pin-merged-ink:#b3d3b5; --pin-merged-edge:#37503d;
    --pin-closed-bg:#1d2b3a; --pin-closed-ink:#a9c8e8; --pin-closed-edge:#2c4560;
    --pin-review-bg: #3a301b; --pin-review-ink: #e2c286; --pin-review-edge: #574526;
    --pin-next-bg:   #1f2c3d; --pin-next-ink:   #a8c4e6; --pin-next-edge:  #2f4460;
    --pin-blocked-bg:#3b2422; --pin-blocked-ink:#e0a89f; --pin-blocked-edge:#59332e;
    --pin-later-bg:  #23282c; --pin-later-ink:  #98a1a9; --pin-later-edge: #333a40;
    --pin-idea-bg:   #1b1f23; --pin-idea-ink:   #838c94; --pin-idea-edge:  #2c3238;
  }}
}}
:root[data-theme="dark"] {{
  --pin-merged-bg:#25352a; --pin-merged-ink:#b3d3b5; --pin-merged-edge:#37503d;
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
  border-bottom: 1px solid var(--rule); padding-bottom: 12px;
  cursor: pointer; list-style: none; user-select: none; }}
/* Стрелка своя: у встроенного маркера ни цвета, ни места не выбрать, а в
   Safari он ещё и рисуется треугольником поверх нашей вёрстки. */
.area-head::-webkit-details-marker {{ display: none; }}
.area-head::marker {{ content: ""; }}
.area-caret {{ width: 9px; height: 9px; margin-top: 11px; flex: none;
  border-right: 2px solid var(--ink-3); border-bottom: 2px solid var(--ink-3);
  transform: rotate(-45deg); transition: transform .15s ease; }}
.area[open] > .area-head .area-caret {{ transform: rotate(45deg); }}
.area-head:hover .area-caret {{ border-color: var(--ink); }}
.area[open] > .area-head {{ border-bottom-color: var(--rule); }}
@media (prefers-reduced-motion: reduce) {{
  .area-caret {{ transition: none; }}
}}
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
.subs {{ list-style: none; margin: 9px 0 0; padding: 8px 0 0;
  border-top: 1px dashed var(--edge); display: flex; flex-direction: column; gap: 6px; }}
.subs li {{ display: flex; gap: 6px; font-size: 12.5px; line-height: 1.35; }}
.subs .box {{ flex: none; opacity: .6; font-size: 12px; line-height: 1.4; }}
.subs li.done {{ opacity: .58; }}
.subs li.done .sub-body {{ text-decoration: line-through;
  text-decoration-thickness: 1px; }}
.subs code {{ font-family: "JetBrains Mono", ui-monospace, monospace; font-size: 9.5px;
  opacity: .6; margin-right: 5px; }}
.sub-note {{ display: block; font-size: 11.5px; opacity: .72; margin-top: 2px;
  text-decoration: none; }}
.needs {{ display: inline-block; font-size: 9.5px; font-weight: 600;
  text-transform: uppercase; letter-spacing: .07em; opacity: .8; }}
.subs .needs {{ display: block; margin-top: 3px; text-decoration: none; }}
.needs code {{ font-size: 9.5px; opacity: 1; margin: 0 0 0 2px; }}
.pin footer {{ margin-top: 9px; padding-top: 7px; border-top: 1px solid var(--edge);
  font-size: 9.5px; font-weight: 600; text-transform: uppercase;
  letter-spacing: .09em; opacity: .68; display: flex; gap: 10px;
  align-items: baseline; flex-wrap: wrap; }}
/* Где искать работу: ветка и коммит рядом со статусом. Читают их вместе —
   «в ревью» без ответа на «где» это состояние, с которым нечего делать. */
.where {{ margin-left: 8px; font-size: 9.5px; letter-spacing: .02em;
  color: inherit; opacity: .75; text-decoration: none; white-space: nowrap; }}
.where:hover {{ opacity: 1; text-decoration: underline; text-underline-offset: 2px; }}
.where code {{ margin-left: 5px; opacity: .8;
  font-family: "JetBrains Mono", ui-monospace, monospace; }}
/* Не записано — не то же самое, что «ветки нет». */
.where.none {{ opacity: .5; font-style: italic; }}
.pin footer .count {{ margin-left: auto; font-variant-numeric: tabular-nums;
  opacity: .85; }}
.pin.hide {{ display: none; }}
/* Свёрнутая карточка. Шапка кликабельна целиком: попадать в маленькую стрелку
   на стикере шириной в колонку — работа, которой человек не просил. */
.pin-head {{ cursor: pointer; list-style: none; display: block;
  position: relative; padding-right: 16px; }}
.pin-head::-webkit-details-marker {{ display: none; }}
.pin-head::marker {{ content: ""; }}
.pin-caret {{ position: absolute; top: 4px; right: 0; width: 6px; height: 6px;
  border-right: 1.6px solid currentColor; border-bottom: 1.6px solid currentColor;
  opacity: .45; transform: rotate(-45deg); transition: transform .15s ease; }}
.pin[open] > .pin-head .pin-caret {{ transform: rotate(45deg); opacity: .7; }}
.pin-head:hover .pin-caret {{ opacity: .9; }}
/* Подзадачи и примечание живут внутри раскрытой части и не должны прилипать
   к подвалу шапки. */
.pin-body > .note:first-child {{ margin-top: 9px; }}
@media (prefers-reduced-motion: reduce) {{ .pin-caret {{ transition: none; }} }}

.s-shipped {{ --bg: var(--pin-shipped-bg); --fg: var(--pin-shipped-ink); --edge: var(--pin-shipped-edge); }}
.s-merged  {{ --bg: var(--pin-merged-bg);  --fg: var(--pin-merged-ink);  --edge: var(--pin-merged-edge); }}
.s-closed  {{ --bg: var(--pin-closed-bg);  --fg: var(--pin-closed-ink);  --edge: var(--pin-closed-edge); }}
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
      <p class="sub">{description}</p>
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
    <span>Source of truth: {source}</span>
    <span>Last change: {updated}</span>
    <span>Built by <code>scripts/acl_board.py</code></span>
  </footer>
</div>

<script>
(function () {{
  /* Области свёрнуты. Шестьдесят задач разом — это стена, по которой нельзя
     вести взглядом; свёрнутая, она за один экран отвечает, из чего состоит
     система и сколько где сделано. Раскрытое человеком запоминается. */
  var AREA_KEY = 'acl.wall.open';
  var PIN_KEY = 'acl.wall.pins';
  var areas = Array.prototype.slice.call(document.querySelectorAll('details.area'));
  var pins = Array.prototype.slice.call(document.querySelectorAll('details.pin'));
  var chips = Array.prototype.slice.call(document.querySelectorAll('.chip'));
  var active = null;
  var known = [];

  function stored(key) {{
    try {{
      var raw = localStorage.getItem(key);
      var list = raw ? JSON.parse(raw) : [];
      return Object.prototype.toString.call(list) === '[object Array]' ? list : [];
    }} catch (e) {{
      return [];   // приватное окно или запрет на хранилище — просто без памяти
    }}
  }}

  /* Один механизм на области и карточки: две разные памяти о том же самом
     разошлись бы, и заметил бы это тот, кто поверил не той. */
  function watch(nodes, key, attr) {{
    var seen = stored(key);
    nodes.forEach(function (n) {{
      if (seen.indexOf(n.getAttribute(attr)) !== -1) n.open = true;
      n.addEventListener('toggle', function () {{
        // Фильтр сам раскрывает области; карточки по-прежнему открывает
        // человек, и их нужно сохранять даже при включённом фильтре.
        if (active && attr === 'data-area') return;
        try {{
          localStorage.setItem(key, JSON.stringify(
            nodes.filter(function (x) {{ return x.open; }})
                 .map(function (x) {{ return x.getAttribute(attr); }})));
        }} catch (e) {{}}
      }});
    }});
    return seen;
  }}

  known = watch(areas, AREA_KEY, 'data-area');
  watch(pins, PIN_KEY, 'data-id');

  function apply() {{
    document.querySelectorAll('.pin').forEach(function (pin) {{
      pin.classList.toggle('hide', !!active && pin.dataset.status !== active);
    }});
    document.querySelectorAll('.cap').forEach(function (cap) {{
      cap.style.display = cap.querySelector('.pin:not(.hide)') ? '' : 'none';
    }});
    areas.forEach(function (a) {{
      if (!active) {{
        a.style.display = '';
        a.open = known.indexOf(a.getAttribute('data-area')) !== -1;
        return;
      }}
      var any = a.querySelector('.pin:not(.hide)');
      a.style.display = any ? '' : 'none';
      /* Найденное надо показать. Свёрнутая область под фильтром выглядит как
         «фильтр ничего не нашёл»: нажатие есть, ответа нет. */
      a.open = !!any;
    }});
  }}

  chips.forEach(function (chip) {{
    chip.setAttribute('aria-pressed', String(chip.dataset.filter === 'all'));
    chip.addEventListener('click', function () {{
      var want = chip.dataset.filter;
      active = (want === 'all' || active === want) ? null : want;
      // All is an explicit recovery action: reveal every area and every card.
      // Toggling an individual status off retains the reader's previous layout.
      if (want === 'all') known = areas.map(function (a) {{ return a.getAttribute('data-area'); }});
      else if (!active) known = stored(AREA_KEY);
      chips.forEach(function (c) {{
        c.setAttribute('aria-pressed', String(c.dataset.filter === (active || 'all')));
      }});
      apply();
    }});
  }});

  /* Ссылка в оглавлении обязана раскрывать область: иначе якорь приводит к
     закрытой шапке, и переход выглядит промахом. */
  document.querySelectorAll('nav.areas a').forEach(function (link) {{
    link.addEventListener('click', function () {{
      var el = document.querySelector(link.getAttribute('href'));
      if (el) el.open = true;
    }});
  }});
  if (location.hash) {{
    var at = document.querySelector(location.hash);
    if (at && at.classList.contains('area')) at.open = true;
  }}
}})();
</script>
"""


# ─────────────────────────── команды ───────────────────────────


#: Копия доски внутри репозитория. Источник живёт в Obsidian, у которого нет
#: ни гита, ни корзины: 8 сентября одна неудачная правка вырезала из доски
#: целую область, и восстановилось это только потому, что незадолго до того
#: была опубликована витрина, а в её HTML осталось содержимое. Удача, а не
#: запасной план.
#:
#: Копия не источник и им не станет: её пишет сборка, читать её как истину
#: нельзя. Она нужна ровно для одного — чтобы у плана работ была история и те
#: же три адреса, что у кода.
SNAPSHOT = Path("docs") / "task-board-snapshot.md"


def snapshot_path(repo: Path) -> Path:
    return repo / SNAPSHOT


def write_snapshot(repo: Path, text: str) -> Path | None:
    """Положить копию доски рядом с кодом. Ошибка не должна ломать сборку.

    Возвращает путь, если файл изменился, иначе `None`: переписывать
    неизменившийся файл значит показывать его в `git status` на каждом
    коммите, и человек перестанет читать это место.
    """
    tmp = None
    try:
        out = snapshot_path(repo)
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.is_file() and out.read_text(encoding="utf-8") == text:
            return None
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=out.parent,
                                         prefix=out.name, suffix=".tmp", delete=False) as f:
            tmp = Path(f.name)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, out)
        return out
    except Exception as exc:  # pragma: no cover - копия важна, но не важнее сборки
        print(f"копию доски записать не удалось: {exc}", file=sys.stderr)
        return None
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def default_out(repo: Path) -> Path:
    raw = os.environ.get(OUT_ENV)
    return Path(raw) if raw else repo / "output" / "acl_task_wall.html"


def cmd_render(args: argparse.Namespace, repo: Path) -> int:
    src = board_path()
    if not src.exists():
        print(f"нет файла доски: {src}", file=sys.stderr)
        return 2
    raw = src.read_text(encoding="utf-8")
    board = parse(raw)
    out = Path(args.out) if args.out else default_out(repo)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(board, landings(raw)), encoding="utf-8")
    if write_snapshot(repo, raw) is not None:
        print(f"копия доски обновлена: {SNAPSHOT.as_posix()} — её надо закоммитить")
    done = sum(1 for t in board.tasks if t.is_done)
    print(f"доска собрана: {out}")
    print(f"  {len(board.areas)} областей, {len(board.tasks)} задач, {done} сделано")
    return 0


def duplicate_ids(board: Board) -> list[str]:
    """Номера, выданные дважды.

    Считается по `all_tasks`, а не по верхнему уровню. Прежняя проверка брала
    только верхний, и 8 сентября я дважды выдал занятый номер подзадаче —
    `acl-142a` и `acl-142b` уже существовали в другом разделе. Сторож при этом
    молчал, потому что смотрел не туда: проверка была, а случай не покрывала.

    Повтор опасен не сам по себе. `by_id` возвращает первое совпадение, значит
    коммит отметит чужую задачу, а в журнал ляжет строка, которая выглядит
    правдой.
    """
    counts = Counter(t.id for t in board.all_tasks)
    return sorted(i for i, count in counts.items() if count > 1)


def cmd_check(args: argparse.Namespace, repo: Path) -> int:
    src = board_path()
    if not src.exists():
        print(f"нет файла доски: {src}", file=sys.stderr)
        return 2
    board = parse(src.read_text(encoding="utf-8"))
    dupes = duplicate_ids(board)
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
    # ветку передаёт хук: скрипт может лежать в другом worktree, и его
    # собственная ветка ничего не говорит о том, куда лёг коммит
    branch = (getattr(args, "branch", "") or "").strip() or branch_of(repo)
    text = src.read_text(encoding="utf-8")
    # Хук — единственное, что запускается само. Проверку `--check` руками не
    # зовут, поэтому о повторе номера сказать больше некому.
    dupes = duplicate_ids(parse(text))
    if dupes:
        print("ВНИМАНИЕ, номер выдан дважды: " + ", ".join(dupes))
        print("  задачи с этими номерами пропущены — исправьте повтор на доске")
    new, ticked = apply_commit(text, sha, message, branch=branch)
    if ticked:
        src.write_text(new, encoding="utf-8")
        where = "shipped" if branch in MAIN_BRANCHES else f"ждёт ревью в {branch}"
        print(f"отмечено ({where}): " + ", ".join(ticked))
    else:
        # Раньше здесь всегда печаталось «в коммите нет id задач». Причина
        # называлась неверно: id мог быть найден и просто уже стоять в нужном
        # состоянии. Человек читал «нет id», шёл искать опечатку в сообщении и
        # переписывал коммит впустую — так и случилось 8 сентября. Ничего не
        # делать — законный исход, но сказать о нём надо правду.
        named = ids_in_commit(message)
        if not named:
            print("в коммите нет id задач — доска не тронута")
        else:
            board = parse(text)
            eligible = [i for i in named if i not in dupes]
            unknown = [i for i in eligible if board.by_id(i) is None]
            if unknown:
                print("на доске нет таких задач: " + ", ".join(unknown))
            unchanged = [i for i in eligible if i not in unknown]
            if unchanged:
                print("уже в этом состоянии: " + ", ".join(unchanged))
    board = parse(new)
    out = default_out(repo)
    out.parent.mkdir(parents=True, exist_ok=True)
    # журнал берём из обновлённого текста: свежая запись сделана только что,
    # и по старому тексту карточка не узнала бы своей ветки
    out.write_text(render(board, landings(new)), encoding="utf-8")
    # Post-commit does not stage files. The owner must explicitly include
    # this snapshot in a later commit; otherwise the backup stays local.
    if write_snapshot(repo, new) is not None:
        print(f"копия доски обновлена: {SNAPSHOT.as_posix()} — добавьте её в следующий коммит явно")
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
    p.add_argument("--branch", default="", help="ветка коммита (её передаёт хук)")
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
