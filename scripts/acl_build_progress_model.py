"""Пересчёт модели процента сборки кампании (ACL-1347) по телеметрии робота.

Точка сборки в билдере показывает примерный процент готовности черновика.
Страница считает его сама, из того же ответа `/publish-jobs/{id}/progress`,
что уже опрашивается раз в 3 с: последний шаг робота и сколько секунд тихо.
Всё, что для этого нужно знать о шагах, лежит одной таблицей в шаблоне —
блок `<script type="application/json" id="cb-build-model">` в
`src/web/templates/report.html`:

* `rules`  — шаг → этап (`profile`, `campaign`, `adset`, `ad`, `final`) или
  `hold` (служебный шаг, встречается на любом этапе: процент стоит). Правила
  пишутся руками, скрипт их не трогает: он по ним раскладывает строки.
* `stages` — типичная длительность этапа в секундах (у `ad` — одного
  объявления). Доли полосы — доли времени: 3 объявления растягивают этап
  объявлений втрое.
* `steps`  — шаг → `[at, next]`: где внутри этапа (внутри одного объявления
  для `ad`) шаг обычно записывается, 0…1, и сколько секунд обычно до
  следующего не служебного шага. По `next` процент плавно растёт, пока
  следующий шаг не записан.

Скрипт перечитывает телеметрию и пересчитывает `stages` и `steps` (медианы).

  python scripts/acl_build_progress_model.py                      # output/rpa_telemetry.jsonl
  python scripts/acl_build_progress_model.py path/to/telemetry.jsonl
  python scripts/acl_build_progress_model.py --write              # обновить блок в шаблоне

Код выхода 1 — в телеметрии есть шаги, которых не знает ни одно правило:
их надо дописать в `rules`, иначе процент на них просто стоит.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = _ROOT / "src" / "web" / "templates" / "report.html"
TELEMETRY = _ROOT / "output" / "rpa_telemetry.jsonl"

STAGES = ("profile", "campaign", "adset", "ad", "final")
HOLD = "hold"

_BLOCK = re.compile(
    r'(<script type="application/json" id="cb-build-model">\s*)(.*?)(\s*</script>)', re.S)
_AD_N = re.compile(r"^ad(\d+)_")


# ── таблица в шаблоне ─────────────────────────────────────────────────────

def read_model(template: Path = TEMPLATE) -> dict[str, Any]:
    m = _BLOCK.search(template.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit(f"{template}: no cb-build-model block")
    return json.loads(m.group(2))


def write_model(model: dict[str, Any], template: Path = TEMPLATE) -> None:
    text = template.read_text(encoding="utf-8")
    body = dump_model(model)
    new = _BLOCK.sub(lambda m: m.group(1) + body + m.group(3), text, count=1)
    template.write_text(new, encoding="utf-8", newline="")


def dump_model(model: dict[str, Any]) -> str:
    """JSON блока: по строке на правило и на шаг — дифф читается глазами."""
    lines = ["{"]
    lines.append(f'  "about": {json.dumps(model.get("about", ""), ensure_ascii=False)},')
    lines.append(f'  "derived": {json.dumps(model.get("derived", {}), ensure_ascii=False)},')
    lines.append(f'  "stages": {json.dumps(model["stages"])},')
    lines.append('  "rules": [')
    lines.append(",\n".join(f"    {json.dumps(r)}" for r in model["rules"]))
    lines.append("  ],")
    lines.append('  "steps": {')
    lines.append(",\n".join(f"    {json.dumps(k)}: {json.dumps(v)}"
                            for k, v in sorted(model["steps"].items())))
    lines.append("  }")
    lines.append("}")
    return "\n".join(lines)


# ── шаг → этап: то же, что делает страница (cbBuild.stageOf / keyOf) ─────

def base_step(step: Any) -> str:
    return str(step or "").split(":", 1)[0].strip()


def stage_of(step: str, rules: list[list[str]]) -> str | None:
    """Этап шага по правилам шаблона; None — шаг не знает ни одно правило."""
    base = base_step(step)
    for pattern, stage in rules:
        if re.search(pattern, base):
            return stage
    return None


def key_of(step: str, note: str = "") -> str:
    """Ключ таблицы `steps`: `ad3_copy` → `ad_copy`, `job_phase` — по заметке."""
    base = base_step(step)
    if base == "job_phase":
        return "job_phase:" + str(note or "")
    return _AD_N.sub("ad_", base)


def ad_index(step: str) -> int | None:
    """Номер объявления из имени шага; None — шаг его не несёт (наследует)."""
    base = base_step(step)
    m = _AD_N.match(base)
    if m:
        return int(m.group(1))
    return 1 if base == "ad_name" else None


# ── разбор телеметрии ─────────────────────────────────────────────────────

def _ts(row: dict[str, Any]) -> float | None:
    try:
        return datetime.fromisoformat(str(row.get("ts"))).timestamp()
    except ValueError:
        return None


def load_rows(path: Path) -> list[dict[str, Any]]:
    rows = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("campaign") and _ts(row) is not None:
                rows.append(row)
    return rows


def attempts(rows: Iterable[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Попытки сборки: строки одной кампании одного задания, одна попытка —
    один `run_id`. Строки задания без `run_id` (профиль, очередь, список
    кампаний) относятся к попытке, которая идёт за ними."""
    by: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by[(str(r.get("job") or ""), str(r["campaign"]))].append(r)
    out: list[list[dict[str, Any]]] = []
    for rs in by.values():
        rs.sort(key=_ts)
        cur: list[dict[str, Any]] | None = None
        cur_id = None
        prefix: list[dict[str, Any]] = []
        for r in rs:
            rid = str(r.get("run_id") or "")
            if not rid:
                prefix.append(r)
                continue
            if cur is None or rid != cur_id:
                cur, cur_id = prefix + [r], rid
                out.append(cur)
            else:
                cur.extend(prefix)
                cur.append(r)
            prefix = []
    return out


def _median(xs: list[float]) -> float:
    return statistics.median(xs) if xs else 0.0


def derive(rows: list[dict[str, Any]], rules: list[list[str]]) -> dict[str, Any]:
    """Медианы длительностей этапов и положения шагов по попыткам."""
    stage_secs: dict[str, list[float]] = defaultdict(list)
    at: dict[str, list[float]] = defaultdict(list)
    nxt: dict[str, list[float]] = defaultdict(list)
    unknown: dict[str, int] = defaultdict(int)
    tries = attempts(rows)
    for rs in tries:
        # Этап строки — не ниже этапа предыдущей: повтор кампании (снова
        # `create`) и служебные шаги этап назад не отматывают.
        cur = 0
        ad = 1
        marks = []   # (строка, ts, этап, номер объявления, ключ | None)
        for r in rs:
            st = stage_of(r.get("step"), rules)
            if st is None:
                unknown[base_step(r.get("step"))] += 1
            key = None
            if st in STAGES:
                cur = max(cur, STAGES.index(st))
                key = key_of(r.get("step"), r.get("note") or "")
            if cur == STAGES.index("ad"):
                n = ad_index(r.get("step"))
                if n is not None:
                    ad = n
            marks.append((r, _ts(r), cur, ad if cur == STAGES.index("ad") else 0, key))
        # Пауза до следующего шага этапа: служебные строки (window_bounds
        # сразу за переходом) процент не двигают — страница на них растит
        # процент от последнего настоящего шага, поэтому и пауза — до него.
        for i, (_r, ts, _st, _ad, key) in enumerate(marks):
            if key is None:
                continue
            later = next((m[1] for m in marks[i + 1:] if m[4] is not None), None)
            if later is not None:
                nxt[key].append(later - ts)
        # Отрезки: этап (а у этапа объявлений — каждое объявление). Отрезок
        # кончается последним своим настоящим шагом (служебная строка за ним
        # его не удлиняет); закончен, если за ним есть настоящий шаг.
        segs: dict[tuple[int, int], list[int]] = defaultdict(list)
        for i, (_r, _ts_, st, ad_n, key) in enumerate(marks):
            if key is not None:
                segs[(st, ad_n)].append(i)
        last_keyed = max((i for idx in segs.values() for i in idx), default=-1)
        order = sorted(segs)
        prev_end = marks[0][1]
        for seg in order:
            idx = segs[seg]
            end = marks[idx[-1]][1]
            done = idx[-1] < last_keyed or marks[idx[-1]][0].get("step") == "ad_level_summary"
            dur = end - prev_end
            if done and dur > 0:
                st_name = STAGES[seg[0]]
                stage_secs[st_name].append(dur)
                for i in idx:
                    key = marks[i][4]
                    if key is not None:
                        at[key].append((marks[i][1] - prev_end) / dur)
            prev_end = end
    stages = [[s, max(1, round(_median(stage_secs[s])))] for s in STAGES]
    steps = {k: [round(_median(v), 2), max(1, round(_median(nxt.get(k, []))))]
             for k, v in at.items()}
    return {"stages": stages, "steps": steps, "attempts": len(tries),
            "samples": {s: len(stage_secs[s]) for s in STAGES}, "unknown": dict(unknown)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("telemetry", nargs="?", default=str(TELEMETRY))
    ap.add_argument("--template", default=str(TEMPLATE))
    ap.add_argument("--write", action="store_true", help="update the table in the template")
    args = ap.parse_args(argv)

    template = Path(args.template)
    model = read_model(template)
    rows = load_rows(Path(args.telemetry))
    got = derive(rows, model["rules"])
    print(f"{len(rows)} campaign rows, {got['attempts']} attempts")
    for (name, secs), (_n, old) in zip(got["stages"], model["stages"]):
        print(f"  {name:9} {secs:5d} s  (was {old}, {got['samples'][name]} samples)")
    if got["unknown"]:
        print("Steps no rule knows (add them to `rules`):")
        for step, n in sorted(got["unknown"].items()):
            print(f"  {step} ×{n}")
    if args.write:
        model["stages"] = got["stages"]
        model["steps"] = got["steps"]
        model["derived"] = {"rows": len(rows), "attempts": got["attempts"],
                            "until": max((str(r.get("ts"))[:10] for r in rows), default="")}
        write_model(model, template)
        print(f"Updated {template}")
    else:
        print(json.dumps({"stages": got["stages"], "steps": got["steps"]}, indent=1)[:4000])
    return 1 if got["unknown"] else 0


if __name__ == "__main__":
    sys.exit(main())
