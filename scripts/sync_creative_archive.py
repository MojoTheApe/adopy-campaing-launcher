"""Наполнение архива статистики (`data/creative_archive.sqlite3`).

Два режима:

* **Ежедневный снапшот** (по умолчанию) — заливает текущий экспорт из
  `config.paths.get_excel_path()`. Это то, что вешается на планировщик Windows.
* **Бэкфилл** (``--backfill``) — заливает исторические выгрузки из папки (обычно
  ``~/Downloads``) в порядке их сохранения. Файлы с чужой схемой пропускаются.

Примеры::

    python scripts/sync_creative_archive.py                       # снапшот
    python scripts/sync_creative_archive.py --backfill            # ~/Downloads
    python scripts/sync_creative_archive.py --backfill "D:\\arch"  # своя папка
    python scripts/sync_creative_archive.py --status              # состояние
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Консоль Windows по умолчанию cp1252 — русский вывод иначе падает UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover — не-tty окружение
        pass

from config.paths import get_excel_path  # noqa: E402
from src.analysis_agent import creative_archive as ca  # noqa: E402

DB_DEFAULT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "creative_archive.sqlite3",
)
#: Мелкие файлы — это не выгрузки статистики, а сводки на пару строк.
MIN_BYTES = 100_000


def _candidate_exports(folder: str) -> list[str]:
    found = glob.glob(os.path.join(folder, "*.xlsx"))
    return [
        p
        for p in found
        if not os.path.basename(p).startswith("~$") and os.path.getsize(p) >= MIN_BYTES
    ]


def _print_status(conn) -> None:
    st = ca.status(conn)
    print(f"строк в архиве : {st.rows:,}")
    print(f"период         : {st.date_min} .. {st.date_max}")
    print(f"снапшотов       : {st.snapshots}")
    if st.gaps:
        head = ", ".join(st.gaps[:8])
        more = f" (+{len(st.gaps) - 8})" if len(st.gaps) > 8 else ""
        print(f"ДЫРЫ ({len(st.gaps)} дн.): {head}{more}")
    else:
        print("дыр нет")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Наполнение архива статистики FB")
    ap.add_argument(
        "--backfill",
        nargs="?",
        const=os.path.join(os.path.expanduser("~"), "Downloads"),
        metavar="FOLDER",
        help="залить исторические выгрузки из папки (по умолчанию ~/Downloads)",
    )
    ap.add_argument("--db", default=DB_DEFAULT, help="путь к SQLite-архиву")
    ap.add_argument("--excel", help="путь к экспорту (переопределяет config/paths)")
    ap.add_argument("--status", action="store_true", help="показать состояние и выйти")
    args = ap.parse_args(argv)

    conn = ca.connect(args.db)
    try:
        if args.status:
            _print_status(conn)
            return 0

        if args.backfill:
            folder = args.backfill
            if not os.path.isdir(folder):
                print(f"нет папки: {folder}", file=sys.stderr)
                return 2
            paths = _candidate_exports(folder)
            print(f"кандидатов в {folder}: {len(paths)}")
            results = ca.ingest_many(conn, paths)
            ok = [r for r in results if not r.skipped]
            print(
                f"\nзалито файлов: {len(ok)} из {len(results)}"
                f" | строк новых {sum(r.inserted for r in ok):,}"
                f", обновлено {sum(r.updated for r in ok):,}"
            )
        else:
            path = args.excel or get_excel_path()
            if not os.path.exists(path):
                print(f"нет файла экспорта: {path}", file=sys.stderr)
                return 2
            res = ca.ingest_file(conn, path)
            if res.skipped:
                print(f"пропуск ({res.source_file}): {res.skipped}")
            else:
                print(
                    f"{res.source_file}: строк {res.rows_read:,},"
                    f" новых {res.inserted:,}, обновлено {res.updated:,},"
                    f" без изменений {res.unchanged:,}"
                    f" | {res.date_min}..{res.date_max}"
                )

        print()
        _print_status(conn)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
