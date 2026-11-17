"""Черновики билдера на машине дашборда (ACL-1681).

Раньше черновики жили только в localStorage браузера: он свой у каждого
профиля Chrome и у каждого адреса (`127.0.0.1` и `localhost` — разные сайты),
а после краха Chrome владелец открыл билдер и увидел пустоту. Теперь копия
лежит в машинной папке `%LOCALAPPDATA%/ACL/builder_drafts.json`, как заметки
аккаунтов (account_notes.py); localStorage остаётся быстрым кэшем.

Три документа: `keep` (карточки кампаний), `jobs` (память заданий публикации),
`ui` (ящик открыт / растянут). У каждого своя метка `saved_at` (мс, как
Date.now() в браузере). Побеждает более новая; проигравшая копия не пропадает —
она лежит в `builder_drafts.<имя>.older.json` (одна, последняя).

Запись атомарная и идёт под машинным замком. Испорченный файл читается как
пустой, а при записи откладывается в `builder_drafts.json.corrupt-<время>`.
"""
from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from config.paths import _atomic_write, get_local_config_dir, machine_file_lock

NAMES = ("keep", "jobs", "ui")
#: Потолок одного документа (байт): черновики — десятки КБ, мегабайты — это мусор.
MAX_DOC_BYTES = 2_000_000

_lock = threading.RLock()


def path() -> Path:
    return Path(get_local_config_dir()) / "builder_drafts.json"


def _older_path(name: str) -> Path:
    return path().with_name(f"builder_drafts.{name}.older.json")


@contextmanager
def _transaction():
    with _lock:
        with machine_file_lock(path().with_suffix(".lock")):
            yield


def _valid_doc(doc) -> bool:
    return (isinstance(doc, dict) and isinstance(doc.get("saved_at"), (int, float))
            and not isinstance(doc.get("saved_at"), bool) and isinstance(doc.get("data"), dict))


def _clean(raw) -> dict[str, dict]:
    docs = raw.get("docs") if isinstance(raw, dict) else None
    if not isinstance(docs, dict):
        raise ValueError("bad shape")
    return {n: d for n, d in docs.items() if n in NAMES and _valid_doc(d)}


def load() -> dict[str, dict]:
    """Все документы. Нет файла или он испорчен — пусто (страница не падает)."""
    try:
        return _clean(json.loads(path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def _load_strict() -> dict[str, dict]:
    """Чтение для записи: нечитаемый файл — отказ (OSError), испорченный —
    откладывается в `.corrupt-<время>` и начинаем с пустого."""
    p = path()
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        return _clean(json.loads(raw))
    except ValueError:
        p.replace(p.with_name(f"{p.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"))
        return {}


def _write(docs: dict[str, dict]) -> None:
    _atomic_write(path(), json.dumps({"docs": docs}, ensure_ascii=False, indent=1,
                                     sort_keys=True).encode("utf-8"))


def _keep_older(name: str, doc: dict) -> None:
    _atomic_write(_older_path(name), json.dumps(doc, ensure_ascii=False, indent=1,
                                                sort_keys=True).encode("utf-8"))


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _touched(entry) -> float:
    return entry["touched"] if isinstance(entry, dict) and _num(entry.get("touched")) else 0


def _cid(campaign) -> str:
    cid = campaign.get("id") if isinstance(campaign, dict) else None
    return cid if isinstance(cid, str) else ""


def _count_campaigns(data: dict) -> int:
    forms = data.get("forms")
    if not isinstance(forms, dict):
        return 0
    return sum(len(e["campaigns"]) for e in forms.values()
               if isinstance(e, dict) and isinstance(e.get("campaigns"), list))


def _normalized(entry):
    """Копия записи формы, где у каждой кампании без `id` он равен `legacy-<номер>`
    (R9: так же называет их клиент при раскладке, см. cbKeep.restore)."""
    if not isinstance(entry, dict) or not isinstance(entry.get("campaigns"), list):
        return entry
    out = dict(entry)
    out["campaigns"] = [
        c if (not isinstance(c, dict) or _cid(c)) else {**c, "id": f"legacy-{i}"}
        for i, c in enumerate(entry["campaigns"])
    ]
    return out


def merge_keep(current: dict, incoming: dict) -> dict:
    """Слить два документа `keep` (ACL-2669): черновики сами не сжимаются.

    Слияние по кампании, а не по форме: на форме побеждает копия с большим
    `touched` (при равенстве — `incoming`), а кампании другой копии, которых
    нет у победителя (по `id`), дописываются в конец. Исчезнуть кампания или
    форма может только по надгробию из `removed` (`"<форма>#<id>"` и
    `"<форма>"` → мс удаления человеком): надгробие не младше копии убирает её,
    а более поздняя правка возвращает. Кампании без `id` (старый формат) перед
    слиянием получают `legacy-<номер>` (R9) и дальше идут как обычные.
    Остальные поля документа берутся из `incoming`."""
    cur_forms = current.get("forms") if isinstance(current.get("forms"), dict) else {}
    inc_forms = incoming.get("forms") if isinstance(incoming.get("forms"), dict) else {}
    cur_forms = {k: _normalized(v) for k, v in cur_forms.items()}
    inc_forms = {k: _normalized(v) for k, v in inc_forms.items()}
    removed: dict = {}
    for src in (current.get("removed"), incoming.get("removed")):
        if isinstance(src, dict):
            for k, ts in src.items():
                if _num(ts) and ts > removed.get(k, float("-inf")):
                    removed[k] = ts
    forms: dict = {}
    for key in list(cur_forms) + [k for k in inc_forms if k not in cur_forms]:
        a, b = cur_forms.get(key), inc_forms.get(key)
        if key in cur_forms and key in inc_forms:
            newer, older = (a, b) if _touched(a) > _touched(b) else (b, a)
        else:
            newer, older = (a, None) if key in cur_forms else (b, None)
        if not isinstance(newer, dict):
            # Не форма билдера (старый/чужой формат): берём как есть.
            if removed.get(key, float("-inf")) < 0:
                forms[key] = newer
            continue
        entry = dict(newer)
        n_camps = newer.get("campaigns")
        o_camps = older.get("campaigns") if isinstance(older, dict) else None
        if isinstance(n_camps, list) or isinstance(o_camps, list):
            n_camps = n_camps if isinstance(n_camps, list) else []
            n_touched, o_touched = _touched(newer), _touched(older)
            kept = [c for c in n_camps
                    if not (_cid(c) and removed.get(f"{key}#{_cid(c)}", float("-inf")) > n_touched)]
            have = {_cid(c) for c in n_camps if _cid(c)}
            for c in (o_camps if isinstance(o_camps, list) else []):
                cid = _cid(c)
                if cid and cid not in have and removed.get(f"{key}#{cid}", float("-inf")) < o_touched:
                    kept.append(c)
                    have.add(cid)
            entry["campaigns"] = kept
            if not kept:
                continue
        if removed.get(key, float("-inf")) >= _touched(entry):
            continue
        forms[key] = entry
    merged = dict(incoming)
    if "forms" in current or "forms" in incoming or forms:
        merged["forms"] = forms
    if removed:
        merged["removed"] = removed
    versions = [d["v"] for d in (current, incoming) if _num(d.get("v"))]
    if versions:
        merged["v"] = max(versions)
    return merged


def save(name: str, saved_at: float, data: dict, base=None) -> tuple[bool, dict]:
    """Записать документ. (принят, документ на сервере после вызова).

    `jobs` и `ui`: входящая копия старше серверной — не принимается (серверная
    новее), а сама уходит в `.older`. Принятая заменяет серверную; если серверная
    при этом отличалась от той, что клиент видел (`base` ≠ её saved_at) и имела
    другое содержимое, она тоже уходит в `.older`: две машины/вкладки писали
    параллельно, и ни одна работа не должна пропасть молча.

    `keep` (ACL-2669): всегда сливается с серверным через `merge_keep`, даже
    если входящий старше; `saved_at` результата — большая из двух меток.
    Принят (True) только если слитое совпало с входящим, иначе клиент получает
    слитый документ и применяет его. Серверная копия до слияния уходит в
    `.older`, если слитое содержит меньше кампаний (возможно лишь по надгробию).
    ValueError — неизвестное имя, форма или размер."""
    if name not in NAMES:
        raise ValueError("unknown document")
    if (not isinstance(saved_at, (int, float)) or isinstance(saved_at, bool)
            or not isinstance(data, dict)):
        raise ValueError("bad document")
    incoming = {"saved_at": saved_at, "data": data}
    if len(json.dumps(incoming, ensure_ascii=False).encode("utf-8")) > MAX_DOC_BYTES:
        raise ValueError("document is too large")
    with _transaction():
        docs = _load_strict()
        stored = docs.get(name)
        if name == "keep" and stored:
            return _save_keep(docs, stored, incoming, base)
        if stored and saved_at < stored["saved_at"]:
            if stored["data"] != data:
                _keep_older(name, incoming)
            return False, stored
        if stored and stored["data"] != data and base != stored["saved_at"]:
            _keep_older(name, stored)
        docs[name] = incoming
        _write(docs)
        return True, incoming


def _save_keep(docs: dict, stored: dict, incoming: dict, base) -> tuple[bool, dict]:
    data = incoming["data"]
    if incoming["saved_at"] < stored["saved_at"]:
        # Серверная копия новее: при равенстве `touched` побеждает она.
        merged_data = merge_keep(data, stored["data"])
        if stored["data"] != data:
            _keep_older("keep", incoming)
    else:
        merged_data = merge_keep(stored["data"], data)
        if stored["data"] != data and base != stored["saved_at"]:
            _keep_older("keep", stored)
    if _count_campaigns(merged_data) < _count_campaigns(stored["data"]):
        _keep_older("keep", stored)
    merged = {"saved_at": max(stored["saved_at"], incoming["saved_at"]), "data": merged_data}
    docs["keep"] = merged
    _write(docs)
    return (merged_data == data and incoming["saved_at"] >= stored["saved_at"]), merged


def load_older(name: str) -> dict | None:
    try:
        doc = json.loads(_older_path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if _valid_doc(doc) else None
