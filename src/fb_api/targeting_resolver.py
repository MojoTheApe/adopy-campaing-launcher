"""Резолвер детального таргетинга: интересы, поведения, должности, работодатели (ACL-1771).

Библиотека таргетов — это строки человека: «Title Case», «Alpari Group or Plus500», поведения,
должности, работодатели. Точный поиск по имени их не находит. Порядок поиска для одного термина:

1. подтверждённый ранее выбор (локальный кэш, `get_local_config_dir()`); id интересов
   перепроверяются тем же пакетным `adinterestvalid` (`interest_fbid_list`), без лишних чтений;
2. точное имя через `adinterestvalid`;
3. `/search type=adinterest q=…`, постранично;
4. список поведений `adTargetingCategory class=behaviors` (один раз на задание), затем
   `adworkposition`, `adworkemployer`, `adeducationschool`,
   `adeducationmajor` (документированные типы поиска);
5. если вся строка не нашлась — делим по « or » / «/» и ищем каждую часть отдельно. Даже когда все
   части найдены точно, разбиение — это ДРУГОЙ таргетинг (OR вместо одного термина), поэтому оно
   предлагается как кандидат и требует разового подтверждения.

Молчаливой подмены нет: автоматически берётся только точное совпадение имени (регистр,
повторные пробелы и разделители «- _ /» не важны; символы + # & . и диакритика СОХРАНЯЮТСЯ).
Всё остальное — «picked X for Y?» с кандидатами: публикация блокируется до подтверждения.
Бюджет считается в чтениях Graph (каждая страница), а не в вызовах get_all; не хватило —
термин «unknown», и человек нажимает Check plan снова. Только чтения: ни одного POST.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import tempfile
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.fb_api.graph import GraphError

#: Куда кладётся тип попадания в `flexible_spec`.
BUCKETS = ("interests", "behaviors", "work_positions", "work_employers",
           "education_schools", "education_majors")
#: Разбиение «A or B»: в кэше это отдельный тип с набором выборов.
SPLIT = "split"
#: Типы поиска Graph для должностей/работодателей/учебных заведений (q=термин, одна страница).
WORK_SEARCHES = (("work_positions", "adworkposition"), ("work_employers", "adworkemployer"))
EDU_SEARCHES = (("education_schools", "adeducationschool"), ("education_majors", "adeducationmajor"))

#: Потолки в ЧТЕНИЯХ Graph (каждая страница = чтение): на искомую строку и на задание.
TERM_READ_CAP = 8
#: Потолок задания растёт с числом искомых строк: JOB_READ_BASE + JOB_READ_PER_TERM * строк
#: (целая строка и каждая её часть), но не выше JOB_READ_ABS_CAP. JOB_READ_CAP - прежнее имя базы.
JOB_READ_BASE = 6
JOB_READ_PER_TERM = 6
JOB_READ_ABS_CAP = 90
JOB_READ_CAP = JOB_READ_BASE
#: Сколько секунд результаты поиска живут между Check plan (повтор после «unknown» идёт дальше).
SEARCH_CACHE_TTL_S = 600.0
#: Сколько чтений оставить клиенту про запас, чтобы наш цикл не съел общий потолок.
CLIENT_RESERVE = 2
SEARCH_PAGES = 2
BEHAVIOR_PAGES = 3
SEARCH_LIMIT = 50
BEHAVIOR_LIMIT = 1000
#: Кандидат: «сильный» (предлагается как «picked X for Y?») и «слабый» (только в списке).
STRONG_SCORE = 0.78
WEAK_SCORE = 0.5
MAX_CANDIDATES = 8
UNKNOWN_MESSAGE = "Couldn't check all interests — press Check plan again"

#: ACL-1920. Какие поиски резолвера НЕОБЯЗАТЕЛЬНЫ: отказ Meta (код 10 «Permission Denied», 200-299)
#: пропускает этот поиск на всё задание и становится предупреждением с названием поиска. Остальные
#: (`adinterest`, `adgeolocation`, `adlocale`) обязательны: без них таргетинг не разрешить безопасно,
#: отказ остаётся ошибкой. Значение: (название для человека, что тогда не сопоставляется).
OPTIONAL_LOOKUPS = {
    "adinterestvalid": ("exact-name interest check", "interests were matched through the interest search instead"),
    "adTargetingCategory": ("behaviors lookup", "behaviors were not matched"),
    "adworkposition": ("work positions lookup", "work positions were not matched"),
    "adworkemployer": ("work employers lookup", "work employers were not matched"),
    "adeducationschool": ("education schools lookup", "education schools were not matched"),
    "adeducationmajor": ("education majors lookup", "education majors were not matched"),
}
REQUIRED_LOOKUPS = ("adinterest", "adgeolocation", "adlocale")


def is_permission_refusal(exc: BaseException) -> bool:
    """Meta отказала по правам: код 10 или семейство 200-299 (в т.ч. 294)."""
    code = getattr(exc, "code", None)
    return isinstance(exc, GraphError) and isinstance(code, int) and (code == 10 or 200 <= code <= 299)


def name_lookup(exc: GraphError, search_type: str) -> GraphError:
    """Та же ошибка, но с типом поиска в тексте (если Graph его ещё не назвал)."""
    if f"search_type={search_type}" in str(exc) or f"lookup={search_type}" in str(exc):
        return exc
    return GraphError(f"{exc} [lookup={search_type}]", code=exc.code, subcode=exc.subcode,
                      status=exc.status, details=exc.details)


def named_search(search_type: str, fn):
    """Вызвать поиск; любая ошибка Graph поднимается с названием этого поиска."""
    try:
        return fn()
    except GraphError as exc:
        raise name_lookup(exc, search_type) from exc


_SPLIT_RE = re.compile(r"\s+or\s+|\s*/\s*|\s*;\s*|\s*,\s*", re.IGNORECASE)
_SEP_RE = re.compile(r"[\s\-_]+")

CACHE_FILE = "targeting_confirmed.json"
_STORE_LOCK = threading.RLock()


class SearchCache:
    """Короткий кэш результатов поиска Meta между Check plan (ключ: область + запрос)."""

    def __init__(self, ttl: float = SEARCH_CACHE_TTL_S, clock=None):
        import time as _time

        self.ttl, self._clock = float(ttl), clock or _time.monotonic
        self._rows: dict[tuple, tuple[float, list[dict]]] = {}
        self._lock = threading.Lock()

    def get(self, scope: str, key: tuple) -> list[dict] | None:
        with self._lock:
            hit = self._rows.get((scope, key))
            if not hit:
                return None
            if self._clock() - hit[0] > self.ttl:
                self._rows.pop((scope, key), None)
                return None
            return hit[1]

    def put(self, scope: str, key: tuple, rows: list[dict]) -> None:
        with self._lock:
            now = self._clock()
            for k in [k for k, (ts, _) in self._rows.items() if now - ts > self.ttl]:
                del self._rows[k]      # просроченное не копим: память ограничена
            self._rows[(scope, key)] = (now, rows)

    def clear(self) -> None:
        with self._lock:
            self._rows.clear()


def norm(text: str) -> str:
    """Имя для сравнения: регистр, повторные пробелы и разделители «- _» не важны.

    «/» не разделитель: это знак разбиения «A/B», и он не равен пробелу («A/B» != «A B»).

    Символы (+ # & . и т.п.) и диакритика сохраняются: «C++» не равно «C», «año» не равно «ano».
    """
    s = unicodedata.normalize("NFC", str(text or "")).casefold()
    return _SEP_RE.sub(" ", s).strip()


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


def split_candidate(parts: list[dict]) -> dict:
    """Разбиение как кандидат: id = id частей через «+», имя = имена через « + »."""
    picks = [{"id": p["id"], "type": p["type"], "name": p["name"]} for p in parts]
    return {"id": "+".join(p["id"] for p in picks), "type": SPLIT,
            "name": " + ".join(p["name"] for p in picks), "picks": picks, "score": 1.0}


def picks_of(entry: dict) -> list[dict]:
    if entry.get("type") == SPLIT:
        return [dict(p) for p in entry.get("picks") or []]
    return [{"id": entry["id"], "type": entry["type"], "name": entry.get("name") or ""}]


class ConfirmedStore:
    """Локальный кэш подтверждённых человеком соответствий: (тип, термин) → id + имя."""

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
                json.dump({"version": 2, "entries": entries}, fh, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    @staticmethod
    def _key(type_: str, term: str) -> str:
        return f"{type_}|{norm(term)}"

    @staticmethod
    def _valid_entry(row: Any) -> bool:
        if not isinstance(row, dict) or not row.get("id"):
            return False
        if row.get("type") == SPLIT:
            return bool(row.get("picks")) and all(
                isinstance(p, dict) and p.get("id") and p.get("type") in BUCKETS for p in row["picks"])
        return row.get("type") in BUCKETS

    def get_all(self, term: str) -> list[dict]:
        """Все подтверждённые выборы термина (по одному на тип)."""
        n = norm(term)
        return [r for k, r in sorted(self._read().items())
                if self._valid_entry(r) and k == self._key(r.get("type"), n)]

    def get(self, term: str) -> dict | None:
        rows = self.get_all(term)
        return rows[0] if rows else None

    def put(self, term: str, *, id: str, type: str, name: str, picks: list[dict] | None = None) -> dict:
        if type != SPLIT and type not in BUCKETS:
            raise ValueError(f"unknown targeting type {type!r}")
        if not norm(term):
            raise ValueError("empty term")
        row: dict[str, Any] = {"term": str(term).strip(), "type": type, "name": str(name),
                               "confirmed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if type == SPLIT:
            if not picks or any(p.get("type") not in BUCKETS or not re.fullmatch(r"\d{1,30}", str(p.get("id")))
                                for p in picks):
                raise ValueError("a split needs numeric picks of known types")
            row["picks"] = [{"id": str(p["id"]), "type": p["type"], "name": str(p.get("name") or "")}
                            for p in picks]
            row["id"] = "+".join(p["id"] for p in row["picks"])
        else:
            if not re.fullmatch(r"\d{1,30}", str(id)):
                raise ValueError("targeting id must be numeric")
            row["id"] = str(id)
        with _STORE_LOCK:
            entries = self._read()
            entries[self._key(type, term)] = row
            self._write(entries)
        return row

    def remove(self, term: str) -> bool:
        """Забыть термин целиком (все типы)."""
        n = norm(term)
        with _STORE_LOCK:
            entries = self._read()
            gone = [k for k in entries if k.split("|", 1)[-1] == n]
            for k in gone:
                entries.pop(k)
            if gone:
                self._write(entries)
        return bool(gone)

    def clear(self) -> int:
        with _STORE_LOCK:
            n = len(self._read())
            if n:
                self._write({})
        return n

    def entries(self) -> list[dict]:
        return sorted((r for r in self._read().values() if self._valid_entry(r)),
                      key=lambda r: (str(r.get("term") or "").casefold(), r["type"]))


# ───────── кандидаты последнего Check plan: подтвердить можно только их ─────────
_CANDIDATES: dict[str, dict[tuple[str, str], dict]] = {}
_CAND_LOCK = threading.Lock()


def register_candidates(items: list[dict]) -> None:
    """Запомнить кандидатов, показанных в Check plan (прежние того же термина заменяются)."""
    fresh: dict[str, dict[tuple[str, str], dict]] = {}
    for item in items or []:
        for it in (item.get("bad_parts") or []) + [item]:
            # A fresh result without candidates must also revoke its old choices.
            choices = fresh.setdefault(norm(it["term"]), {})
            rows = list(it.get("candidates") or [])
            if it.get("suggested"):
                rows.append(it["suggested"])
            for c in rows:
                if c.get("id") and c.get("type"):
                    choices[(str(c["id"]), str(c["type"]))] = dict(c)
    with _CAND_LOCK:
        _CANDIDATES.update(fresh)


def candidate_for(term: str, id: str, type: str) -> dict | None:
    with _CAND_LOCK:
        return (_CANDIDATES.get(norm(term)) or {}).get((str(id), str(type)))


def forget_candidates() -> None:
    with _CAND_LOCK:
        _CANDIDATES.clear()


class _Spent(Exception):
    """Вышел потолок чтений термина/задания (не ошибка Meta)."""


_OK_STATES = ("exact", "confirmed")


class TermResolver:
    """Ищет термины библиотеки в Meta. Кэш на задание; только чтения."""

    def __init__(self, client: Any, store: ConfirmedStore | None = None, *,
                 term_cap: int = TERM_READ_CAP, job_cap: int | None = None,
                 cache: SearchCache | None = None, scope: str = ""):
        self.client = client
        self.store = store if store is not None else ConfirmedStore()
        self.term_cap = int(term_cap)
        #: Явный потолок задания (тесты) или None: тогда он растёт с числом строк (см. prefetch).
        self._fixed_job_cap = None if job_cap is None else int(job_cap)
        self.job_cap = self._fixed_job_cap if job_cap is not None else JOB_READ_BASE + JOB_READ_PER_TERM
        self._shared, self._scope = cache, scope
        #: Чтений Graph за задание (каждая страница), для бюджета и тестов.
        self.reads = 0
        self._term_reads = 0
        self._results: dict[str, dict] = {}
        self._valid: dict[str, dict] = {}
        self._valid_asked: set[str] = set()
        self._valid_ids: set[str] = set()
        self._ids_checked = False
        self._behaviors: list[dict] | None = None
        self._query_cache: dict[tuple, list[dict]] = {}
        #: ACL-1920: необязательные поиски, в которых Meta отказала (тип -> текст), без повторов.
        self.refused: dict[str, str] = {}
        self._ids_unverified = False
        #: Interest ids Meta rejected as `Invalid id` (their cache entries are dropped).
        self._bad_ids: set[str] = set()

    # -- необязательный поиск: отказ по правам пропускает его, а не роняет весь план
    def _lookup(self, search_type: str, params: dict, pages: int, *, count_term: bool = True) -> list[dict]:
        if search_type in self.refused:
            return []                      # уже отказано в этом задании: повторов нет
        optional = search_type in OPTIONAL_LOOKUPS
        before = getattr(self.client, "calls", None)
        try:
            return self._get_all(params, pages, count_term=count_term)
        except GraphError as exc:
            if not (optional and is_permission_refusal(exc)):
                raise name_lookup(exc, search_type) from exc
        # Отказанный запрос тоже был чтением: он остаётся в бюджете.
        after = getattr(self.client, "calls", None)
        spent = max(1, after - before) if isinstance(before, int) and isinstance(after, int) else 1
        self.reads += spent
        if count_term:
            self._term_reads += spent
        label, effect = OPTIONAL_LOOKUPS[search_type]
        self.refused[search_type] = f"Meta refused the {label} ({search_type}) on this account; {effect}."
        return []

    def warnings(self) -> list[str]:
        """Предупреждения Check plan: какой именно поиск Meta отказала (английский текст)."""
        out = list(self.refused.values())
        if self._ids_unverified:
            out.append("Confirmed interest picks were not re-checked against Meta "
                       "(the exact-name interest check was refused).")
        return out

    def _blind_spots(self) -> str:
        """Отказанные поиски, где термин мог бы найтись (adinterestvalid не в счёт: его дублирует adinterest)."""
        gone = [OPTIONAL_LOOKUPS[t][0] for t in self.refused if t != "adinterestvalid"]
        if not gone:
            return ""
        return ("Meta refused the " + " and the ".join(gone) + " on this account, so the term could not be "
                "checked there; fix the term or confirm a candidate")

    # -- бюджет чтений
    def _get_all(self, params: dict, pages: int, *, count_term: bool = True) -> list[dict]:
        key = (tuple(sorted((k, json.dumps(v, sort_keys=True)) for k, v in params.items())), pages)
        if key in self._query_cache:
            return self._query_cache[key]
        if self._shared is not None:
            warm = self._shared.get(self._scope, key)
            if warm is not None:     # результат недавнего Check plan: ни одного чтения
                self._query_cache[key] = warm
                return warm
        # Наихудший случай: все страницы. Не хватает - не начинаем (результат «unknown», не догадка).
        if self.reads + pages > self.job_cap or (count_term and self._term_reads + pages > self.term_cap):
            raise _Spent
        cl = self.client
        mx, used = getattr(cl, "max_calls", None), getattr(cl, "calls", None)
        if isinstance(mx, int) and isinstance(used, int) and used + CLIENT_RESERVE + pages > mx:
            raise _Spent
        before = used if isinstance(used, int) else None
        rows = cl.get_all("/search", params, max_pages=pages)
        after = getattr(cl, "calls", None)
        # Настоящие чтения: разница счётчика клиента (по странице на GET); без счётчика - как минимум одно.
        spent = (after - before) if before is not None and isinstance(after, int) else 1
        spent = max(1, spent)
        self.reads += spent
        if count_term:
            self._term_reads += spent
        self._query_cache[key] = rows
        if self._shared is not None:
            self._shared.put(self._scope, key, rows)
        return rows

    # -- пакет adinterestvalid: имена + перепроверка id подтверждённых интересов
    def _ask_valid(self, names: list[str], ids: list[str] | None = None) -> None:
        todo = [n for n in dict.fromkeys(names) if norm(n) not in self._valid_asked]
        ids = [i for i in dict.fromkeys(ids or []) if i not in self._valid_ids]
        if "adinterestvalid" in self.refused:
            self._ids_unverified = self._ids_unverified or bool(ids)
            return
        if not todo and not ids:
            return
        params: dict[str, Any] = {"type": "adinterestvalid"}
        if todo:
            params["interest_list"] = todo
        if ids:
            params["interest_fbid_list"] = ids
        try:
            rows = self._lookup("adinterestvalid", params, 1)
        except GraphError as exc:
            bad = self._rejected_ids(exc, ids)
            if not bad:
                raise
            # A cached interest id that Meta calls "Invalid id" (e.g. 61, never a real 13+ digit id) is
            # dropped from the confirmed cache so the term is searched again, not failing the plan check.
            self._drop_cached_ids(bad)
            self._bad_ids.update(bad)
            self._ask_valid(names, [i for i in ids if i not in bad])
            return
        if "adinterestvalid" in self.refused:
            self._ids_unverified = bool(ids)    # поиск adinterest ниже найдёт точные имена сам
            return
        for n in todo:
            self._valid_asked.add(norm(n))
        if ids:
            self._ids_checked = True
        for r in rows:
            if r.get("valid") is not False and r.get("id"):
                self._valid_ids.add(str(r["id"]))
                nm = str(r.get("name") or "")
                if nm:
                    self._valid[norm(nm)] = {"id": str(r["id"]), "type": "interests", "name": nm}

    @staticmethod
    def _rejected_ids(exc: Exception, asked: list[str]) -> set[str]:
        """Ids Meta called `Invalid id` in an adinterestvalid answer; all asked ids if it names none."""
        text = str(exc)
        if "invalid id" not in text.casefold():
            return set()
        named = set(re.findall(r"(?i)invalid id[:\s#]*([0-9]+)", text)) & set(asked)
        return named or set(asked)

    def _drop_cached_ids(self, bad: set[str]) -> None:
        for e in self.store.entries():
            if any(p["type"] == "interests" and str(p["id"]) in bad for p in picks_of(e)):
                self.store.remove(str(e.get("term") or ""))

    def _cached_interest_ids(self, terms: list[str]) -> list[str]:
        ids: list[str] = []
        for t in terms:
            for e in self.store.get_all(t):
                ids += [p["id"] for p in picks_of(e) if p["type"] == "interests" and p["id"] not in self._bad_ids]
        return ids

    def prefetch(self, terms: list[str]) -> None:
        """Один пакетный adinterestvalid на все термины, их части и id подтверждённых интересов."""
        self._term_reads = 0
        try:
            todo = [t for t in terms if norm(t) not in self._results]
            every = todo + [p for t in todo for p in split_term(t)]
            if self._fixed_job_cap is None:
                n = len({norm(x) for x in every}) + len(self._results)
                self.job_cap = min(JOB_READ_ABS_CAP, max(self.job_cap, JOB_READ_BASE + JOB_READ_PER_TERM * n))
            self._ask_valid(every, self._cached_interest_ids(every))
        except _Spent:
            pass

    @staticmethod
    def _row(r: dict, bucket: str) -> dict | None:
        if not r.get("id") or not r.get("name") or r.get("valid") is False:
            return None
        return {"id": str(r["id"]), "type": bucket, "name": str(r["name"])}

    def _stages(self, term: str):
        """Ленивый перебор групп строк (bucket, rows) в порядке резолва."""
        # adinterest обязателен: отказ Meta здесь остаётся ошибкой (с названием поиска).
        yield "interests", self._lookup("adinterest", {"type": "adinterest", "q": term, "limit": SEARCH_LIMIT},
                                        SEARCH_PAGES)
        if self._behaviors is None:
            # Общий список поведений: на задание один раз, в потолок термина не входит.
            self._behaviors = self._lookup(
                "adTargetingCategory", {"type": "adTargetingCategory", "class": "behaviors", "limit": BEHAVIOR_LIMIT},
                BEHAVIOR_PAGES, count_term=False)
        yield "behaviors", self._behaviors
        for bucket, typ in WORK_SEARCHES + EDU_SEARCHES:
            yield bucket, self._lookup(typ, {"type": typ, "q": term, "limit": SEARCH_LIMIT}, 1)

    def _find(self, term: str) -> dict:
        """Одна строка целиком: подтверждённый -> точный -> кандидаты."""
        key = norm(term)
        self._term_reads = 0     # потолок считается на каждую искомую строку (целую и каждую часть)
        stale = ""
        saved = self.store.get_all(term)
        if saved:
            ok = [e for e in saved if not self._stale(e)]
            if ok:
                return {"term": term, "state": "confirmed", "candidates": [], "reason": "",
                        "picks": [p for e in ok for p in picks_of(e)]}
            stale = "the confirmed pick is no longer returned by Meta; confirm it again"
        cands: dict[tuple, dict] = {}
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
            return {"term": term, "state": "unknown", "picks": [], "candidates": [],
                    "reason": UNKNOWN_MESSAGE}
        ranked = sorted(cands.values(), key=lambda c: (-c["score"], c["type"], c["name"]))[:MAX_CANDIDATES]
        reason = "; ".join(x for x in (stale, self._blind_spots()) if x)
        return {"term": term, "state": "unresolved", "picks": [], "candidates": ranked, "reason": reason}

    def _stale(self, entry: dict) -> bool:
        """Интерес из кэша, которого Meta больше не отдаёт (проверено пакетом). Иные типы - доверяем."""
        if not self._ids_checked:
            return False
        return any(p["type"] == "interests" and p["id"] not in self._valid_ids for p in picks_of(entry))

    def resolve(self, term: str) -> dict:
        """Результат по термину: state exact|confirmed|pending|unresolved|unknown."""
        key = norm(term)
        if key in self._results:
            return self._results[key]
        res = self._find(term)
        parts = split_term(term)
        if res["state"] == "unresolved" and parts:
            subs = [self._find(p) if norm(p) != key else res for p in parts]
            if any(s["state"] == "unknown" for s in subs):
                res = {**res, "state": "unknown", "reason": UNKNOWN_MESSAGE, "parts": subs}
            elif all(s["state"] in _OK_STATES for s in subs):
                # Разбиение - другой таргетинг (OR): предлагаем кандидатом, ждём подтверждения.
                cand = split_candidate([p for s in subs for p in s["picks"]])
                res = {"term": term, "state": "pending", "picks": [], "candidates": [cand],
                       "suggested": cand, "reason": "", "split": True, "parts": subs}
            else:
                bad = [self._classify(s) for s in subs if s["state"] not in _OK_STATES]
                res = {**self._classify(res), "parts": [self._classify(s) for s in subs], "bad_parts": bad}
        res = self._classify(res)
        self._results[key] = res
        return res

    @staticmethod
    def _classify(res: dict) -> dict:
        """unresolved с сильным кандидатом -> pending («picked X for Y?»)."""
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
            item = {k: r[k] for k in ("term", "state", "picks", "candidates", "reason", "suggested", "split")
                    if k in r}
            if r.get("bad_parts"):
                item["bad_parts"] = [{k: p[k] for k in ("term", "state", "candidates", "reason", "suggested")
                                      if k in p} for p in r["bad_parts"]]
            out.append(item)
        return out

    def needs_attention(self) -> list[dict]:
        """Термины/части, которые нельзя использовать без человека."""
        out: list[dict] = []
        for r in self._results.values():
            if r["state"] in _OK_STATES:
                continue
            out += r.get("bad_parts") or [r]
        return out
