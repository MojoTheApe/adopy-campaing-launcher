"""Добавить в общую библиотеку то, чего в ней нет (ACL-1241, ACL-2038).

Логика seed лежит в src, а не в scripts: её вызывает и дешборд при подключении
библиотеки (кнопка «Connect team library»), а установленная сборка скриптов
может не нести. Скрипт `scripts/seed_team_library.py` — тонкая обёртка.
Добавляются только недостающие записи: ничего не перезаписывается и не удаляется.
"""
from __future__ import annotations

from src.analysis_agent import ad_text_registry as ar
from src.analysis_agent import targeting_registry as tr
from src.analysis_agent import team_library as tl
from src.fb_api import form_templates as ft

KINDS = {
    "targets": (tr.snapshot, "targets", tr.normalise_one,
                lambda r: tr.fingerprint(r.get("interests", ""), r.get("narrow", ""))),
    "ad_texts": (ar.snapshot, "texts", ar.normalise_one,
                 lambda r: ar.fingerprint(r.get("headline", ""), r.get("body", ""), r.get("description", ""))),
    # Preserve the existing ACL-2054 CLI seed kind when moving shared code to src.
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


def seed(kind: str, *, add_missing: bool, dry_run: bool, local: dict | None = None) -> tuple[int, list[str]]:
    """`local` — снимок реестра машины, снятый заранее (ACL-2038): после подключения любое
    открытие раздела подтягивает общую копию поверх местной, и снимок по месту был бы уже чужой."""
    snapshot, key, _, _ = KINDS[kind]
    local = local if local is not None else snapshot()
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
