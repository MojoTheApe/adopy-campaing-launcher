"""Свернуть фрагменты docs/system/changelog.d/ACL-NNNN.md в docs/system/CHANGELOG.md.

Каждая задача кладёт запись в свой файл-фрагмент, а не в верх общего CHANGELOG.md:
так параллельные PR не конфликтуют на одной и той же строке. Фрагменты сворачивает
пакетное вливание или выпуск — одним коммитом, когда PR не плодятся.

    python scripts/fold_changelog.py            # свернуть и удалить фрагменты
    python scripts/fold_changelog.py --check    # проверить формат, ничего не менять
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
FRAGMENTS = Path("docs/system/changelog.d")
CHANGELOG = Path("docs/system/CHANGELOG.md")
HEADER = "# System history\n\n"
# Допускается суффикс (ACL-N-followup.md): повторная запись по той же задаче.
FILE_NAME = re.compile(r"^ACL-(\d+)(?:-[A-Za-z0-9-]+)?\.md$")
HEADING = re.compile(r"^## ACL-(\d+) — .+ — (\d{4}-\d{2}-\d{2})$")


def read_fragments(directory: Path) -> list[tuple[str, int, str, Path]]:
    """(дата, номер, текст, путь); ошибка формата — ValueError."""
    found = []
    for path in sorted(directory.glob("*.md")):
        match = FILE_NAME.match(path.name)
        if path.name.lower() == "readme.md":
            continue
        if not match:
            raise ValueError(f"{path.name}: expected the name ACL-NNNN.md or ACL-NNNN-suffix.md")
        text = path.read_text(encoding="utf-8-sig").strip("\n")
        first = text.split("\n", 1)[0]
        heading = HEADING.match(first)
        if not heading:
            raise ValueError(f"{path.name}: first line must be '## ACL-NNNN — Title — YYYY-MM-DD' (use ' — ', an em dash)")
        if heading.group(1) != match.group(1):
            raise ValueError(f"{path.name}: heading number {heading.group(1)} differs from the file name")
        found.append((heading.group(2), int(heading.group(1)), text, path))
    return found


def fold(root: Path = _ROOT, *, check: bool = False) -> list[str]:
    """Вернуть номера свёрнутых задач; при check ничего не пишет."""
    fragments = read_fragments(root / FRAGMENTS)
    if not fragments or check:
        return [f"ACL-{number}" for _, number, _, _ in fragments]
    changelog = root / CHANGELOG
    body = changelog.read_text(encoding="utf-8-sig")
    if not body.startswith(HEADER):
        raise ValueError(f"{CHANGELOG} must start with '{HEADER.strip()}'")
    rest = body[len(HEADER):]
    # Новые записи выше старых: по дате, при равенстве — по номеру задачи.
    ordered = sorted(fragments, key=lambda item: (item[0], item[1]), reverse=True)
    # Повторный запуск после частичного сбоя не должен дублировать запись; сравниваем текст
    # целиком, иначе новая запись по уже упомянутой задаче потерялась бы вместе с фрагментом.
    fresh = [item for item in ordered if item[2] not in rest]
    if fresh:
        block = "\n\n".join(item[2] for item in fresh)
        changelog.write_text(HEADER + block + "\n\n" + rest, encoding="utf-8", newline="\n")
    for _, _, _, path in fragments:
        path.unlink()
    return [f"ACL-{number}" for _, number, _, _ in ordered]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="validate fragments only")
    parser.add_argument("--root", type=Path, default=_ROOT)
    args = parser.parse_args(argv)
    try:
        folded = fold(args.root, check=args.check)
    except (ValueError, OSError) as error:
        print(f"fold_changelog: {error}", file=sys.stderr)
        return 1
    verb = "valid" if args.check else "folded"
    print(f"{verb}: {', '.join(folded) if folded else 'no fragments'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
