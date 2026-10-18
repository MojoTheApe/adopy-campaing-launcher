"""Резолвер детального таргетинга: интересы, поведения, должности, работодатели (ACL-1771).

Библиотека таргетов — это строки человека: «Title Case», «Alpari Group or Plus500», поведения,
должности, работодатели. Точный поиск по имени их не находит. Порядок поиска для одного термина:

1. подтверждённый ранее выбор (локальный кэш, `get_local_config_dir()`); id интересов
   перепроверяются тем же пакетным `adinterestvalid` (`interest_fbid_list`), без лишних чтений;
2. точное имя через `adinterestvalid`;
3. `/search type=adinterest q=…`, постранично;
4. список поведений `adTargetingCategory class=behaviors` (один раз на задание), затем
   `adworkposition`, `adworkemployer`, а если уже есть близкие кандидаты — `adeducationschool`,
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

#: Куда кладётся тип попадания в `flexible_spec`.
BUCKETS = ("interests", "behaviors", "work_positions", "work_employers",
           "education_schools", "education_majors")
#: Разбиение «A or B»: в кэше это отдельный тип с набором выборов.
SPLIT = "split"
#: Типы поиска Graph для должностей/работодателей/учебных заведений (q=термин, одна страница).
WORK_SEARCHES = (("work_positions", "adworkposition"), ("work_employers", "adworkemployer"))
EDU_SEARCHES = (("education_schools", "adeducationschool"), ("education_majors", "adeducationmajor"))

#: Необязательные поиски: отказ Meta по правам не валит проверку, этот тип пропускается на всё задание.
#: Ключ - тип поиска Graph, значение - (как назвать поиск человеку, что из-за этого не подобрано), English.
OPTIONAL_SEARCHES = {
    "adinterestvalid": ("interest validation", "interests are matched through the interest search instead; "
                         "previously confirmed interests were not re-verified"),
    "adTargetingCategory": ("behaviors", "behaviors were not matched"),
    "adworkposition": ("work positions", "work positions were not matched"),
    "adworkemployer": ("work employers", "work employers were not matched"),
    "adeducationschool": ("education schools", "education schools were not matched"),
    "adeducationmajor": ("education majors", "education majors were not matched"),
}
#: Коды Graph «нет прав»: 10 (Permission Denied), 200 (permission error), 294 (нужно разрешение Marketing API).
PERMISSION_CODES = frozenset({10, 200, 294})

#: Потолки в ЧТЕНИЯХ Graph (каждая страница = чтение): на искомую строку и на задание.
TERM_READ_CAP = 8
JOB_READ_CAP = 16
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

_SPLIT_RE = re.compile(r"\s+or\s+|\s*/\s*|\s*;\s*|\s*,\s*", re.IGNORECASE)
_SEP_RE = re.compile(r"[\s\-_/]+")

CACHE_FILE = "targeting_confirmed.json"


def norm(text: str) -> str:
    """Имя для сравнения: регистр, повторные пробелы и разделители «- _ /» не важны.

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
        entries = self._read()
        entries[self._key(type, term)] = row
        self._write(entries)
        return row

    def remove(self, term: str) -> bool:
        """Забыть термин целиком (все типы)."""
        n = norm(term)
        entries = self._read()
        gone = [k for k in entries if k.split("|", 1)[-1] == n]
        for k in gone:
            entries.pop(k)
        if gone:
            self._write(entries)
        return bool(gone)

    def clear(self) -> int:
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
            rows = list(it.get("candidates") or [])
            if it.get("suggested"):
                rows.append(it["suggested"])
            for c in rows:
                if c.get("id") and c.get("type"):
                    fresh.setdefault(norm(it["term"]), {})[(str(c["id"]), str(c["type"]))] = dict(c)
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


class _Refused(Exception):
    """Meta отказала в необязательном поиске (код прав); тип уже запомнен, повторов не будет."""


def is_permission_refusal(exc: BaseException) -> bool:
    """Ошибка Graph с кодом «нет прав» (10 / 200 / 294)."""
    return getattr(exc, "code", None) in PERMISSION_CODES


def with_search_type(exc: Exception, search_type: str) -> Exception:
    """Та же ошибка Graph, но с названием поиска в тексте: человек видит, какой запрос отказан."""
    from src.fb_api.graph import GraphError
    if not isinstance(exc, GraphError) or f"search_type={search_type}" in str(exc):
        return exc
    return GraphError(f"{exc} [search_type={search_type}]", code=exc.code, subcode=exc.subcode,
                      status=exc.status, details=getattr(exc, "details", None))


_OK_STATES = ("exact", "confirmed")


class TermResolver:
    """Ищет термины библиотеки в Meta. Кэш на задание; только чтения."""

    def __init__(self, client: Any, store: ConfirmedStore | None = None, *,
                 term_cap: int = TERM_READ_CAP, job_cap: int = JOB_READ_CAP):
        self.client = client
        self.store = store if store is not None else ConfirmedStore()
        self.term_cap, self.job_cap = int(term_cap), int(job_cap)
        #: Чтений Graph за задание (каждая страница), для бюджета и тестов.
        self.reads = 0
        self._term_reads = 0
        self._results: dict[str, dict] = {}
        self._valid: dict[str, dict] = {}
        self._valid_asked: set[str] = set()
        self._valid_ids: set[str] = set()
        #: id интересов, реально отправленных в успешный пакет adinterestvalid; и те, что Meta явно назвала valid=false.
        self._ids_asked: set[str] = set()
        self._ids_denied: set[str] = set()
        self._behaviors: list[dict] | None = None
        #: Типы поиска, в которых Meta отказала по правам: на задание, больше не спрашиваем.
        self._refused: dict[str, str] = {}
        #: Какие отказанные типы затронули ТЕКУЩИЙ черновик (предупреждения - по черновикам), и по термину.
        self._draft_skips: set[str] = set()
        self._skip_acc: set[str] = set()
        self._key_skips: dict[str, set[str]] = {}
        self._query_cache: dict[tuple, list[dict]] = {}

    # -- бюджет чтений
    def _get_all(self, params: dict, pages: int, *, count_term: bool = True) -> list[dict]:
        key = (tuple(sorted((k, json.dumps(v, sort_keys=True)) for k, v in params.items())), pages)
        if key in self._query_cache:
            return self._query_cache[key]
        # Наихудший случай: все страницы. Не хватает - не начинаем (результат «unknown», не догадка).
        if self.reads + pages > self.job_cap or (count_term and self._term_reads + pages > self.term_cap):
            raise _Spent
        cl = self.client
        mx, used = getattr(cl, "max_calls", None), getattr(cl, "calls", None)
        if isinstance(mx, int) and isinstance(used, int) and used + CLIENT_RESERVE + pages > mx:
            raise _Spent
        before = used if isinstance(used, int) else None
        stype = str(params.get("type") or "")
        try:
            rows = cl.get_all("/search", params, max_pages=pages)
        except Exception as exc:  # noqa: BLE001 - разбираем только ошибки Graph
            from src.fb_api.graph import GraphError
            if not isinstance(exc, GraphError):
                raise
            # Отказ тоже стоил чтение: учитываем в бюджете, как и успешный запрос.
            now = getattr(cl, "calls", None)
            lost = (now - before) if before is not None and isinstance(now, int) else 1
            self.reads += max(1, lost)
            if count_term:
                self._term_reads += max(1, lost)
            if stype in OPTIONAL_SEARCHES and is_permission_refusal(exc):
                self._refused[stype] = str(exc)
                self._note_skip(stype)
                raise _Refused(stype) from exc
            raise with_search_type(exc, stype) from exc
        after = getattr(cl, "calls", None)
        # Настоящие чтения: разница счётчика клиента (по странице на GET); без счётчика - как минимум одно.
        spent = (after - before) if before is not None and isinstance(after, int) else 1
        spent = max(1, spent)
        self.reads += spent
        if count_term:
            self._term_reads += spent
        self._query_cache[key] = rows
        return rows

    # -- пакет adinterestvalid: имена + перепроверка id подтверждённых интересов
    def _ask_valid(self, names: list[str], ids: list[str] | None = None) -> None:
        if "adinterestvalid" in self._refused:
            self._note_skip("adinterestvalid")
            return           # Meta отказала: интересы ищутся обычным adinterest, пакет не повторяем
        todo = [n for n in dict.fromkeys(names) if norm(n) not in self._valid_asked]
        ids = [i for i in dict.fromkeys(ids or []) if i not in self._valid_ids]
        if not todo and not ids:
            return
        params: dict[str, Any] = {"type": "adinterestvalid"}
        if todo:
            params["interest_list"] = todo
        if ids:
            params["interest_fbid_list"] = ids
        try:
            rows = self._get_all(params, 1)
        except _Refused:
            return
        for n in todo:
            self._valid_asked.add(norm(n))
        self._ids_asked.update(ids)
        for r in rows:
            if r.get("valid") is False and r.get("id"):
                self._ids_denied.add(str(r["id"]))
            if r.get("valid") is not False and r.get("id"):
                self._valid_ids.add(str(r["id"]))
                nm = str(r.get("name") or "")
                if nm:
                    self._valid[norm(nm)] = {"id": str(r["id"]), "type": "interests", "name": nm}

    def _cached_interest_ids(self, terms: list[str]) -> list[str]:
        ids: list[str] = []
        for t in terms:
            for e in self.store.get_all(t):
                ids += [p["id"] for p in picks_of(e) if p["type"] == "interests"]
        return ids

    def prefetch(self, terms: list[str]) -> None:
        """Один пакетный adinterestvalid на все термины, их части и id подтверждённых интересов."""
        self._term_reads = 0
        try:
            todo = [t for t in terms if norm(t) not in self._results]
            every = todo + [p for t in todo for p in split_term(t)]
            self._ask_valid(every, self._cached_interest_ids(every))
        except _Spent:
            pass

    @staticmethod
    def _row(r: dict, bucket: str) -> dict | None:
        if not r.get("id") or not r.get("name") or r.get("valid") is False:
            return None
        return {"id": str(r["id"]), "type": bucket, "name": str(r["name"])}

    def _optional(self, typ: str, params: dict, pages: int, *, count_term: bool = True) -> list[dict]:
        """Необязательный поиск: отказ по правам -> пустой список (тип пропущен до конца задания)."""
        if typ in self._refused:
            self._note_skip(typ)
            return []
        try:
            return self._get_all(params, pages, count_term=count_term)
        except _Refused:
            return []

    def _stages(self, term: str, near):
        """Ленивый перебор групп строк (bucket, rows) в порядке резолва."""
        yield "interests", self._get_all({"type": "adinterest", "q": term, "limit": SEARCH_LIMIT}, SEARCH_PAGES)
        if self._behaviors is None and "adTargetingCategory" not in self._refused:
            # Общий список поведений: на задание один раз, в потолок термина не входит.
            rows = self._optional(
                "adTargetingCategory",
                {"type": "adTargetingCategory", "class": "behaviors", "limit": BEHAVIOR_LIMIT},
                BEHAVIOR_PAGES, count_term=False)
            if "adTargetingCategory" not in self._refused:
                self._behaviors = rows
        if "adTargetingCategory" in self._refused:
            self._note_skip("adTargetingCategory")
        yield "behaviors", self._behaviors or []
        for bucket, typ in WORK_SEARCHES:
            yield bucket, self._optional(typ, {"type": typ, "q": term, "limit": SEARCH_LIMIT}, 1)
        if near():       # учебные заведения и специальности - только когда есть хоть что-то близкое
            for bucket, typ in EDU_SEARCHES:
                yield bucket, self._optional(typ, {"type": typ, "q": term, "limit": SEARCH_LIMIT}, 1)

    def _note_skip(self, typ: str) -> None:
        self._draft_skips.add(typ)
        self._skip_acc.add(typ)

    def begin_draft(self) -> None:
        """Новый черновик: предупреждения считаются заново, память об отказах (без повторов) остаётся."""
        self._draft_skips = set()

    def refusals(self) -> list[str]:
        """Предупреждения (English) об отказанных поисках, которые затронули текущий черновик."""
        out = []
        for typ in self._refused:
            if typ not in self._draft_skips:
                continue
            label, effect = OPTIONAL_SEARCHES[typ]
            out.append(f"Meta refused the {label} lookup ({typ}) on this account; {effect}.")
        return out

    def _blind_spots(self) -> str:
        """Что не проверено из-за отказов (adinterestvalid не в счёт: его заменяет adinterest)."""
        return ", ".join(OPTIONAL_SEARCHES[t][0] for t in self._refused if t != "adinterestvalid")

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
            stale_ids = {p["id"] for e in saved if self._stale(e) for p in picks_of(e) if p["type"] == "interests"}
            stale_name = next((p["name"] for e in saved for p in picks_of(e) if p["id"] in stale_ids), term)
        cands: dict[tuple, dict] = {}
        try:
            self._ask_valid([term])
            hit = self._valid.get(key)
            if hit:
                return {"term": term, "state": "exact", "picks": [hit], "candidates": [], "reason": ""}

            def near() -> bool:
                return bool(cands)

            for bucket, rows in self._stages(term, near):
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
        if stale:
            # Повторный Confirm того же id ничего не изменит (Meta его не подтверждает): не предлагаем по кругу.
            cands = {k: c for k, c in cands.items() if not (c["type"] == "interests" and c["id"] in stale_ids)}
        ranked = sorted(cands.values(), key=lambda c: (-c["score"], c["type"], c["name"]))[:MAX_CANDIDATES]
        if stale and not ranked:
            return {"term": term, "state": "rejected", "picks": [], "candidates": [], "action": "remove",
                    "reason": self._rejected_message(stale_name, term, stale_ids)}
        blind = self._blind_spots()
        if blind:
            note = f"Meta refused the {blind} lookup, so '{term}' could not be checked there"
            if not ranked:
                # Могло найтись только через отказанный поиск: не «не найдено», а «не проверено».
                # blind: «A or B» всё равно разбиваем (ACL-1933) - части могут найтись целиком.
                return {"term": term, "state": "unknown", "picks": [], "candidates": [], "reason": note,
                        "blind": True}
            stale = f"{stale}; {note}" if stale else note
        return {"term": term, "state": "unresolved", "picks": [], "candidates": ranked, "reason": stale}

    def _unconfirmed(self) -> set[str]:
        """id интересов, которые спрашивали пакетом, а Meta не подтвердила (нет в ответе или valid=false)."""
        return self._ids_asked - self._valid_ids

    def _stale(self, entry: dict) -> bool:
        """Интерес из кэша, который проверили пакетом и Meta не подтвердила. Не проверяли - доверяем."""
        bad = self._unconfirmed()
        return any(p["type"] == "interests" and p["id"] in bad for p in picks_of(entry))

    def _rejected_message(self, name: str, term: str, ids: set[str]) -> str:
        """Текст (English): Meta явно отказала или проверка не вернула id (не путаем «нет» и «не удалось проверить»)."""
        if ids & self._ids_denied:
            return (f"Meta no longer accepts the interest '{name}' for targeting (it may be retired or "
                    f"restricted). Remove '{term}' from this ad set or pick another interest.")
        return (f"Could not verify the interest '{name}' with Meta (the validity check did not confirm it), "
                f"so it cannot be used. Press Check plan again, remove '{term}' from this ad set or pick "
                f"another interest.")

    def resolve(self, term: str) -> dict:
        """Результат по термину: state exact|confirmed|pending|unresolved|unknown."""
        key = norm(term)
        if key in self._results:
            self._draft_skips |= self._key_skips.get(key, set())      # результат из кэша: отказ тот же
            return self._results[key]
        self._skip_acc = set()
        res = self._find(term)
        parts = split_term(term)
        if parts and (res["state"] == "unresolved" or res.get("blind")):
            subs = [self._find(p) if norm(p) != key else res for p in parts]
            if any(s["state"] == "unknown" for s in subs):
                why = next((s["reason"] for s in subs if s["state"] == "unknown" and s.get("reason")),
                           UNKNOWN_MESSAGE)
                res = {**res, "state": "unknown", "reason": why, "parts": subs}
            elif all(s["state"] in _OK_STATES for s in subs):
                # Разбиение - другой таргетинг (OR): предлагаем кандидатом, ждём подтверждения.
                cand = split_candidate([p for s in subs for p in s["picks"]])
                res = {"term": term, "state": "pending", "picks": [], "candidates": [cand],
                       "suggested": cand, "reason": "", "split": True, "parts": subs}
            else:
                bad = [self._classify(s) for s in subs if s["state"] not in _OK_STATES]
                res = {**self._classify(res), "parts": [self._classify(s) for s in subs], "bad_parts": bad}
        res = self._classify(res)
        self._key_skips[key] = set(self._skip_acc)
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
