"""Генерация заметок дневника креативов в Obsidian.

Из архива (`data/creative_archive.sqlite3`) собирает по каждой стране:

* ``Креативы — {Страна} — Свод вердиктов`` — перезаписывается целиком;
* ``Креативы — {Страна} — Дневник`` и ``— Портрет ЦА`` — создаются один раз и
  дальше не трогаются (там живёт ручное);
* ``Креативы — 00 Индекс`` — карта заметок.

Примеры::

    python scripts/build_creative_notes.py --country Colombia
    python scripts/build_creative_notes.py --min-spend 50000
    python scripts/build_creative_notes.py --country Colombia --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover
        pass

from config import paths  # noqa: E402
from src.analysis_agent import creative_archive as ca  # noqa: E402
from src.analysis_agent import creative_notes as cn  # noqa: E402
from src.analysis_agent import creative_verdicts as cv  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_DEFAULT = os.path.join(REPO, "data", "creative_archive.sqlite3")


def _write(path: str, text: str, *, overwrite: bool, dry: bool) -> str:
    exists = os.path.exists(path)
    if exists and not overwrite:
        return "уже есть, не трогаю"
    if dry:
        return f"[dry-run] {'перезаписал бы' if exists else 'создал бы'} ({len(text):,} симв.)"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return "перезаписан" if exists else "создан"


def _countries(conn, explicit: list[str] | None, min_spend: float) -> list[str]:
    if explicit:
        return explicit
    rows = conn.execute(
        "SELECT country, SUM(spend) s FROM fact GROUP BY country"
        " HAVING s >= ? ORDER BY s DESC",
        (min_spend,),
    ).fetchall()
    return [r["country"] for r in rows]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Заметки дневника креативов в Obsidian")
    ap.add_argument("--country", action="append", help="страна (можно несколько раз)")
    ap.add_argument("--db", default=DB_DEFAULT)
    ap.add_argument("--vault", default=None,
                    help="папка заметок в vault (по умолчанию ACL_OBSIDIAN_DIR или известные места)")
    ap.add_argument(
        "--min-spend", type=float, default=50_000,
        help="порог спенда для авто-выбора стран (по умолчанию 50000)",
    )
    ap.add_argument("--as-of", help="дата отсчёта зрелости (по умолчанию макс. в архиве)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if not args.vault:
        found = paths.get_obsidian_dashboard_dir()
        args.vault = str(found) if found else ""
        if not found:
            print(paths.OBSIDIAN_MISSING_WARNING, file=sys.stderr)

    if not os.path.exists(args.db):
        print(f"нет архива: {args.db}\nсначала: python scripts/sync_creative_archive.py --backfill",
              file=sys.stderr)
        return 2
    if not args.dry_run and not os.path.isdir(args.vault):
        print(f"нет папки vault: {args.vault or '(не найдена)'}", file=sys.stderr)
        return 2

    conn = ca.connect(args.db)
    try:
        st = ca.status(conn)
        as_of = args.as_of or st.date_max
        if not as_of:
            print("архив пуст", file=sys.stderr)
            return 2

        countries = _countries(conn, args.country, args.min_spend)
        if not countries:
            print("не нашёл стран под условие", file=sys.stderr)
            return 2

        print(f"архив: {st.rows:,} строк, {st.date_min}..{st.date_max},"
              f" снапшотов {st.snapshots}, дыр {len(st.gaps)}")
        print(f"отсчёт зрелости на: {as_of}\n")

        lag = cv.measure_revenue_lag(conn)
        if lag.get("samples"):
            print(f"лаг выручки: медиана {lag['median']:.0f} дн., p90 {lag['p90']},"
                  f" p95 {lag['p95']} (наблюдений {lag['samples']:,})\n")

        done: list[str] = []
        for country in countries:
            bundles, stats = cv.load_bundles(conn, country)
            if not bundles:
                print(f"{country}: связок нет, пропуск")
                continue
            gaps = [g for g in st.gaps if stats.date_min and g >= stats.date_min]
            digest = cn.render_digest(
                country, bundles, stats, as_of=as_of, archive_gaps=gaps, lag=lag,
            )
            base = os.path.join(args.vault, f"Креативы — {country}")
            r1 = _write(f"{base} — Свод вердиктов.md", digest, overwrite=True, dry=args.dry_run)
            r2 = _write(f"{base} — Дневник.md", cn.render_diary_seed(country),
                        overwrite=False, dry=args.dry_run)
            r3 = _write(f"{base} — Портрет ЦА.md", cn.render_audience_seed(country),
                        overwrite=False, dry=args.dry_run)
            mature = sum(1 for b in bundles.values() if cv.is_mature(b, stats, as_of))
            print(
                f"{country}: связок {len(bundles):,} (дозрело {mature})"
                f" | свод: {r1} | дневник: {r2} | портрет: {r3}"
            )
            done.append(country)

        if done:
            archive_note = (
                f"- **Файл:** `data/creative_archive.sqlite3` (в git не попадает)\n"
                f"- **Строк:** {st.rows:,}  ·  **период:** {st.date_min} .. {st.date_max}"
                f"  ·  **снапшотов:** {st.snapshots}\n"
                f"- **Дыр в архиве:** {len(st.gaps)} дн.\n"
                "- **Наполнение:** `scripts/sync_creative_archive.py` (планировщик Windows,"
                " раз в сутки)\n"
                "- **Пересборка сводов:** `scripts/build_creative_notes.py`"
            )
            idx = cn.render_index(done, archive_note)
            r = _write(os.path.join(args.vault, "Креативы — 00 Индекс.md"), idx,
                       overwrite=True, dry=args.dry_run)
            print(f"\nиндекс: {r}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
