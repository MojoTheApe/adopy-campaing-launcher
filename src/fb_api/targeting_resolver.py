"""Резолвер детального таргетинга: интересы, поведения, должности, работодатели (ACL-1771).

Библиотека таргетов — это строки человека: «Title Case», «Alpari Group or Plus500», поведения,
должности, работодатели. Точный поиск по имени их не находит. Порядок поиска для одного термина:

1. подтверждённый ранее выбор (локальный кэш, `get_local_config_dir()`);
2. точное имя через `adinterestvalid` (регистр и пунктуация не важны);
3. `/search type=adinterest q=…`, постранично;
4. классы `adTargetingCategory`: behaviors, work_positions, work_employers,
   education_schools, education_majors — постранично;
5. если вся строка не нашлась — делим по « or » / «/» и ищем каждую часть отдельно.

Молчаливой подмены нет: автоматически берётся только точное совпадение имени (без учёта
регистра и пунктуации). Всё остальное — «picked X for Y?» с кандидатами: публикация
блокируется, пока человек не подтвердит выбор; подтверждённый выбор запоминается и в
следующий раз берётся без вопроса. Только чтения: ни одного POST.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: Куда кладётся тип попадания в `flexible_spec`.
BUCKETS = ("interests", "behaviors", "work_positions", "work_employers",
           "education_schools", "education_majors")
#: Классы `adTargetingCategory`, которые просматриваются после интересов (порядок = приоритет).
CATEGORY_CLASSES = ("behaviors", "work_positions", "work_employers",
                    "education_schools", "education_majors")

#: Потолок вызовов на один термин (вместе с его частями) и на всё задание.
TERM_CALL_CAP = 12
JOB_CALL_CAP = 60
#: Сколько вызовов оставить клиенту про запас, чтобы наш цикл не съел общий потолок.
CLIENT_RESERVE = 2
#: Страниц на один запрос: интересы/классы по q — до 2; полный список поведений — до 5.
SEARCH_PAGES = 2
BEHAVIOR_PAGES = 5
SEARCH_LIMIT = 50
BEHAVIOR_LIMIT = 1000
#: Кандидат: «сильный» (предлагается как «picked X for Y?») и «слабый» (только в списке).
STRONG_SCORE = 0.78
WEAK_SCORE = 0.5
MAX_CANDIDATES = 8

_SPLIT_RE = re.compile(r"\s+or\s+|\s*/\s*|\s*;\s*|\s*,\s*", re.IGNORECASE)

CACHE_FILE = "targeting_confirmed.json"


def norm(text: str) -> str:
    """Имя для сравнения: регистр и пунктуация не важны, пробелы схлопнуты."""
    s = unicodedata.normalize("NFKD", str(text or "")).casefold()
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[\W_]+", " ", s).split())


def split_term(term: str) -> list[str]:
    """«A or B», «A/B» → ["A", "B"]; одна часть — деления нет."""
    parts = [p.strip() for p in _SPLIT_RE.split(str(term or "")) if p.strip()]
    return parts if len(parts) > 1 else []


def score(term: str, name: str) -> float:
    """Близость имени к термину 0..1 (нечёткая, только для списка кандидатов)."""
    a, b = norm(term), norm(name)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    if ta and tb and (ta <= tb or tb <= ta):
        ratio = max(ratio, 0.8)
    return round(ratio, 3)


class ConfirmedStore:
    """Локальный кэш подтверждённых человеком соответствий: термин → id + тип + имя."""

    def __init__(self, path: str | os.PathLike | None = None):
        self._path = Path(path) if path else None

    @property
    def path(self) -> Path:
        if self._path is not None:
            return self._path
        from config import paths

        return Path(paths.get_local_config_dir()) / CACHE_FILE

    def _read(self) -> dict[str, dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        entries = data.get("entries") if isinstance(data, dict) else None
        return entries if isinstance(entries, dict) else {}

    def _write(self, entries: dict[str, dict]) -> None:
        p = self.path
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".tgt-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "entries": entries}, fh, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def get(self, term: str) -> dict | None:
        row = self._read().get(norm(term))
        if isinstance(row, dict) and row.get("id") and row.get("type") in BUCKETS:
            return row
        return None

    def put(self, term: str, *, id: str, type: str, name: str) -> dict:
        if type not in BUCKETS:
            raise ValueError(f"unknown targeting type {type!r}")
        if not re.fullmatch(r"\d{1,30}", str(id)):
            raise ValueError("targeting id must be numeric")
        key = norm(term)
        if not key:
            raise ValueError("empty term")
        entries = self._read()
        entries[key] = {"term": str(term).strip(), "id": str(id), "type": type, "name": str(name),
                        "confirmed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self._write(entries)
        return entries[key]

    def remove(self, term: str) -> bool:
        entries = self._read()
        if entries.pop(norm(term), None) is None:
            return False
        self._write(entries)
        return True

    def clear(self) -> int:
        n = len(self._read())
        if n:
            self._write({})
        return n

    def entries(self) -> list[dict]:
        return sorted(self._read().values(), key=lambda r: str(r.get("term") or "").casefold())


class _Spent(Exception):
    """Вышел потолок вызовов термина/задания (не ошибка Meta)."""


class TermResolver:
    """Ищет термины библиотеки в Meta. Кэш на задание; только чтения."""

    def __init__(self, client: Any, store: ConfirmedStore | None = None, *,
                 term_cap: int = TERM_CALL_CAP, job_cap: int = JOB_CALL_CAP):
        self.client = client
        self.store = store if store is not None else ConfirmedStore()
        self.term_cap, self.job_cap = int(term_cap), int(job_cap)
        #: Вызовов get_all за задание (счётчик для бюджета и тестов).
        self.calls = 0
        self._term_calls = 0
        self._results: dict[str, dict] = {}
        self._valid: dict[str, dict] = {}
        self._valid_asked: set[str] = set()
        self._class_lists: dict[str, list[dict]] = {}
        self._query_cache: dict[tuple, list[dict]] = {}

    # -- бюджет вызовов
    def _get_all(self, params: dict, pages: int) -> list[dict]:
        key = (tuple(sorted((k, json.dumps(v, sort_keys=True)) for k, v in params.items())), pages)
        if key in self._query_cache:
            return self._query_cache[key]
        if self.calls >= self.job_cap or self._term_calls >= self.term_cap:
            raise _Spent
        cl = self.client
        mx, used = getattr(cl, "max_calls", None), getattr(cl, "calls", None)
        if isinstance(mx, int) and isinstance(used, int) and used + CLIENT_RESERVE + pages > mx:
            raise _Spent
        self.calls += 1
        self._term_calls += 1
        rows = cl.get_all("/search", params, max_pages=pages)
        self._query_cache[key] = rows
        return rows

    # -- стадии
    def _ask_valid(self, names: list[str]) -> None:
        todo = [n for n in dict.fromkeys(names) if norm(n) not in self._valid_asked]
        if not todo:
            return
        for n in todo:
            self._valid_asked.add(norm(n))
        for r in self._get_all({"type": "adinterestvalid", "interest_list": todo}, 1):
            if r.get("valid") is not False and r.get("id"):
                nm = str(r.get("name") or "")
                self._valid[norm(nm)] = {"id": str(r["id"]), "type": "interests", "name": nm}

    def prefetch(self, terms: list[str]) -> None:
        """Один пакетный вызов adinterestvalid на все термины (как раньше), без съедания потолка термина."""
        self._term_calls = 0
        try:
            todo = [t for t in terms if norm(t) not in self._results and not self.store.get(t)]
            # Части «A or B» едут в тот же пакет: точные части потом не стоят ни одного вызова.
            self._ask_valid(todo + [p for t in todo for p in split_term(t)])
        except _Spent:
            pass

    @staticmethod
    def _row(r: dict, bucket: str) -> dict | None:
        if not r.get("id") or not r.get("name"):
            return None
        if r.get("valid") is False:
            return None
        return {"id": str(r["id"]), "type": bucket, "name": str(r["name"])}

    def _stages(self, term: str):
        """Ленивый перебор групп строк (bucket, rows) в порядке резолва."""
        yield "interests", self._get_all({"type": "adinterest", "q": term, "limit": SEARCH_LIMIT}, SEARCH_PAGES)
        for cls in CATEGORY_CLASSES:
            if cls == "behaviors":
                if cls not in self._class_lists:
                    self._class_lists[cls] = self._get_all(
                        {"type": "adTargetingCategory", "class": cls, "limit": BEHAVIOR_LIMIT}, BEHAVIOR_PAGES)
                yield cls, self._class_lists[cls]
            else:
                yield cls, self._get_all(
                    {"type": "adTargetingCategory", "class": cls, "q": term, "limit": SEARCH_LIMIT}, SEARCH_PAGES)

    def _find(self, term: str) -> dict:
        """Одна строка целиком: подтверждённый → точный → кандидаты."""
        key = norm(term)
        self._term_calls = 0     # потолок считается на каждую искомую строку (целую и каждую часть)
        saved = self.store.get(term)
        if saved:
            return {"term": term, "state": "confirmed",
                    "picks": [{"id": saved["id"], "type": saved["type"], "name": saved["name"]}],
                    "candidates": [], "reason": ""}
        cands: dict[tuple, dict] = {}
        reason = ""
        try:
            self._ask_valid([term])
            hit = self._valid.get(key)
            if hit:
                return {"term": term, "state": "exact", "picks": [hit], "candidates": [], "reason": ""}
            for bucket, rows in self._stages(term):
                for r in rows:
                    c = self._row(r, bucket)
                    if not c:
                        continue
                    if norm(c["name"]) == key:
                        return {"term": term, "state": "exact", "picks": [c], "candidates": [], "reason": ""}
                    sc = score(term, c["name"])
                    if sc >= WEAK_SCORE:
                        cands.setdefault((c["type"], c["id"]), {**c, "score": sc})
        except _Spent:
            reason = "the call limit for this term was reached before every Meta list was searched"
        ranked = sorted(cands.values(), key=lambda c: (-c["score"], c["type"], c["name"]))[:MAX_CANDIDATES]
        return {"term": term, "state": "unresolved", "picks": [], "candidates": ranked, "reason": reason}

    def resolve(self, term: str) -> dict:
        """Результат по термину: state exact|confirmed|split|pending|unresolved."""
        key = norm(term)
        if key in self._results:
            return self._results[key]
        self._term_calls = 0
        res = self._find(term)
        if res["state"] == "unresolved":
            parts = split_term(term)
            if parts:
                subs = [self._find(p) if norm(p) != key else res for p in parts]
                if all(s["state"] in ("exact", "confirmed") for s in subs):
                    res = {"term": term, "state": "split", "reason": "",
                           "picks": [p for s in subs for p in s["picks"]], "candidates": [],
                           "parts": subs}
                else:
                    # Не все части найдены: показываем только непонятые части с их кандидатами.
                    bad = [self._classify(s) for s in subs if s["state"] not in ("exact", "confirmed")]
                    res = {**self._classify(res), "parts": [self._classify(s) for s in subs],
                           "bad_parts": bad}
        if res["state"] == "unresolved":
            res = self._classify(res)
        self._results[key] = res
        return res

    @staticmethod
    def _classify(res: dict) -> dict:
        """unresolved с сильным кандидатом → pending («picked X for Y?»)."""
        if res["state"] != "unresolved":
            return res
        best = res["candidates"][0] if res["candidates"] else None
        if best and best["score"] >= STRONG_SCORE:
            return {**res, "state": "pending", "suggested": best}
        return res

    # -- отчёт для Check plan
    def report(self) -> list[dict]:
        """Все термины задания: как найдены, кандидаты, что ждёт подтверждения."""
        out = []
        for r in self._results.values():
            item = {k: r[k] for k in ("term", "state", "picks", "candidates", "reason") if k in r}
            if "suggested" in r:
                item["suggested"] = r["suggested"]
            if r.get("bad_parts"):
                item["bad_parts"] = [{k: p[k] for k in ("term", "state", "candidates", "reason", "suggested")
                                      if k in p} for p in r["bad_parts"]]
            out.append(item)
        return out

    def needs_attention(self) -> list[dict]:
        """Части, которые нельзя использовать без человека: непонятые термины/части."""
        out: list[dict] = []
        for r in self._results.values():
            if r["state"] in ("exact", "confirmed", "split"):
                continue
            if r.get("bad_parts"):
                out += r["bad_parts"]
            else:
                out.append(r)
        return out
