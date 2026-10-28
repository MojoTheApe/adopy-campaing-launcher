"""Render docs/SETUP-NEW-PC.md into the offline guide shipped with the installer.

ACL-1175: страница установки должна быть у человека в руках вместе с
программой, а не жить за доступом к репозиторию, который она же и выдаёт.
Поэтому при сборке Markdown превращается в один самодостаточный HTML, он
кладётся в бандл и открывается ярлыком «Setup guide» и кнопкой мастера.

Отдельной библиотеки Markdown в сборке нет, и тащить её ради одной страницы
незачем: разбирается ровно то подмножество, которым страница написана
(заголовки, абзацы, вложенные списки, чекбоксы, таблицы, жирный/курсив/код).
Всё, что разобрать не удалось, тест ловит по оставшимся знакам разметки.
"""
from __future__ import annotations

import argparse
import html
import re
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
DEFAULT_SOURCE = REPO_ROOT / "docs" / "SETUP-NEW-PC.md"
GUIDE_FILE_NAME = "setup-guide.html"

_LIST_ITEM = re.compile(r"^(\s*)([-*]|\d+\.)\s+(.*)$")
_HEADING = re.compile(r"^(#{1,3})\s+(.*)$")
_TABLE_RULE = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?$")


def _inline(text: str) -> str:
    # Код вырезается до экранирования и разметки, чтобы `**` или `<version>`
    # внутри него остались буквами.
    pieces = re.split(r"(`[^`]+`)", text)
    out = []
    for piece in pieces:
        if len(piece) > 1 and piece.startswith("`") and piece.endswith("`"):
            out.append(f"<code>{html.escape(piece[1:-1])}</code>")
            continue
        value = html.escape(piece, quote=False)
        value = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", value)
        value = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", value)
        out.append(value)
    return "".join(out)


def _list_item_text(text: str) -> str:
    if text.startswith("[ ] "):
        return '<span class="box">☐</span> ' + _inline(text[4:])
    if text.lower().startswith("[x] "):
        return '<span class="box">☑</span> ' + _inline(text[4:])
    return _inline(text)


def _render_list(lines: list[str]) -> str:
    items: list[list] = []  # [indent, number or None, text]
    for line in lines:
        match = _LIST_ITEM.match(line)
        if match:
            marker = match.group(2)
            number = int(marker[:-1]) if marker[0].isdigit() else None
            items.append([len(match.group(1)), number, match.group(3).strip()])
        elif items:
            items[-1][2] += " " + line.strip()
    out: list[str] = []
    stack: list[tuple[int, str]] = []
    for indent, number, text in items:
        tag = "ul" if number is None else "ol"
        while stack and indent < stack[-1][0]:
            out.append(f"</li></{stack.pop()[1]}>")
        if stack and indent == stack[-1][0]:
            out.append("</li>")
        else:
            # Нумерация, прерванная абзацем, продолжается с того же номера.
            out.append(f'<ol start="{number}">' if number not in (None, 1) else f"<{tag}>")
            stack.append((indent, tag))
        task = text[:4].lower() in ("[ ] ", "[x] ")
        out.append(f'<li class="task">{_list_item_text(text)}' if task else f"<li>{_list_item_text(text)}")
    while stack:
        out.append(f"</li></{stack.pop()[1]}>")
    return "".join(out)


def _table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _render_table(lines: list[str]) -> str:
    rows = [line for line in lines if not _TABLE_RULE.match(line.strip())]
    head, body = rows[0], rows[1:]
    parts = ["<table><thead><tr>"]
    parts += [f"<th>{_inline(cell)}</th>" for cell in _table_cells(head)]
    parts.append("</tr></thead><tbody>")
    for row in body:
        parts.append("<tr>" + "".join(f"<td>{_inline(cell)}</td>" for cell in _table_cells(row)) + "</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def render_body(markdown: str) -> str:
    lines = markdown.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue
        heading = _HEADING.match(stripped)
        if heading:
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            index += 1
            continue
        if stripped == "---":
            out.append("<hr>")
            index += 1
            continue
        if stripped.startswith("|"):
            block = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                block.append(lines[index])
                index += 1
            out.append(_render_table(block))
            continue
        if _LIST_ITEM.match(line):
            block = []
            while index < len(lines) and lines[index].strip():
                block.append(lines[index])
                index += 1
            out.append(_render_list(block))
            continue
        block = []
        while index < len(lines):
            current = lines[index].strip()
            if (not current or current == "---" or current.startswith("|")
                    or _HEADING.match(current) or _LIST_ITEM.match(lines[index])):
                break
            block.append(current)
            index += 1
        out.append(f"<p>{_inline(' '.join(block))}</p>")
    return "\n".join(out)


_STYLE = """
body{margin:0;background:#f5f7fb;color:#273248;font:16px/1.55 "Segoe UI",Arial,sans-serif}
main{max-width:820px;margin:0 auto;padding:32px 24px 64px;background:#fff;min-height:100vh;box-sizing:border-box}
h1{color:#172036;font-size:28px;margin:0 0 12px}
h2{color:#172036;font-size:21px;margin:28px 0 10px}
h3{font-size:17px;margin:20px 0 8px}
hr{border:0;border-top:1px solid #dde3ee;margin:28px 0}
code{background:#eef2f8;border-radius:4px;padding:1px 5px;font:14px Consolas,monospace}
li{margin:4px 0}
.box{font-size:18px}
li.task{list-style:none;margin-left:-1.1em}
table{border-collapse:collapse;width:100%;margin:12px 0}
th,td{border:1px solid #dde3ee;padding:8px 10px;text-align:left;vertical-align:top}
th{background:#eef2f8}
@media print{body{background:#fff}main{padding:0}}
"""


def render_page(markdown: str, *, version: str = "") -> str:
    title = "Adopy Campaign Launcher — Setup guide"
    stamp = f'<p style="color:#6b7690;font-size:13px">Shipped with version {html.escape(version)}.</p>' if version else ""
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body><main>\n{render_body(markdown)}\n{stamp}</main></body></html>\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", default="")
    args = parser.parse_args()
    page = render_page(args.source.read_text(encoding="utf-8"), version=args.version)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(page, encoding="utf-8")
    print(f"Setup guide written: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
