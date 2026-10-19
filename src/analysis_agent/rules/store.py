"""Хранилище Rules: rules.json, rules_settings.json и три журнала JSONL.

Дизайн: C:\\acl-notes\\rules-design.md, разделы 5 и 6. Все файлы лежат в
`config.paths.get_local_config_dir()` (машинные, в гит не идут).

Гарантии:
- JSON пишется атомарно (temp + fsync + os.replace), прошлая копия остаётся в .bak;
- журналы JSONL только дописываются, каждая строка с fsync; оборванный хвост
  (процесс убили посреди записи) отбрасывается при чтении и вычищается перед
  следующей записью;
- каждая мутация - цикл «прочитать с диска, изменить, записать» под файловой
  блокировкой, поэтому два процесса дешборда не затирают друг друга;
- версии правил неизменяемы: правка дописывает новую версию.
"""
from __future__ import annotations

import contextlib
import copy
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from config.paths import get_local_config_dir, machine_file_lock

from . import model

log = logging.getLogger(__name__)

RULES_FILE = "rules.json"
SETTINGS_FILE = "rules_settings.json"
EVALUATIONS_FILE = "rule_evaluations.jsonl"
ACTIONS_FILE = "rule_actions.jsonl"
HOLDS_FILE = "rule_holds.jsonl"
LOCK_FILE = "rules.lock"

SCHEMA_VERSION = 1

# Жёсткий замок Automatic (решение владельца: Automatic выключен, пока он не ответит на
# вопросы T5). Менять только после этого решения; тесты включают замок через env.
AUTOMATIC_AVAILABLE = False
AUTOMATIC_ENV = "ACL_RULES_AUTOMATIC"
AUTOMATIC_UNAVAILABLE_MESSAGE = "Automatic mode is not available yet."


def automatic_available() -> bool:
    return AUTOMATIC_AVAILABLE or os.environ.get(AUTOMATIC_ENV) == "1"

DEFAULT_SETTINGS: dict = {
    # Глобальный kill switch (дизайн 5.1, п.2). Включается только осознанно.
    "schema_version": SCHEMA_VERSION,
    "automatic_enabled": False,
    "limits": {"per_account_day": 3, "per_machine_day": 10, "per_rule_day": 5},
    "freshness": {"bi_max_age_hours": 36, "snapshot_max_age_hours": 24},
    "changed_at": None,
    "changed_by": None,
}


class RulesStoreError(Exception):
    pass


class SchemaTooNew(RulesStoreError):
    """Файл записан более новой версией программы: не читаем вслепую и не перезаписываем."""


class RuleNotFound(RulesStoreError):
    pass


class VersionConflict(RulesStoreError):
    """Правило успели поменять: ожидаемая версия не совпала с текущей."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- межпроцессная блокировка -------------------------------------------------

_thread_lock = threading.RLock()
_held = threading.local()


@contextlib.contextmanager
def _file_lock(path: Path, timeout: float = 30.0) -> Iterator[None]:
    """Эксклюзивная блокировка: RLock для потоков + config.paths.machine_file_lock для процессов. Реентерабельна."""
    with _thread_lock:
        depth = getattr(_held, "depth", 0)
        if depth:
            _held.depth = depth + 1
            try:
                yield
            finally:
                _held.depth -= 1
            return
        try:
            cm = machine_file_lock(path, timeout=timeout)
            cm.__enter__()
        except TimeoutError as exc:
            raise RulesStoreError(f"could not lock {path} in {timeout}s") from exc
        _held.depth = 1
        try:
            yield
        finally:
            _held.depth = 0
            cm.__exit__(None, None, None)


# --- низкоуровневые файлы ----------------------------------------------------

def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        if path.is_file():
            # последняя целая копия - на случай, если сам файл когда-нибудь окажется битым
            bak = path.with_name(path.name + ".bak")
            bak_tmp = path.with_name(f".{path.name}.bak.{uuid.uuid4().hex}.tmp")
            try:
                with open(bak_tmp, "wb") as bfh:
                    bfh.write(path.read_bytes())
                    bfh.flush()
                    os.fsync(bfh.fileno())
                os.replace(bak_tmp, bak)
            except OSError:
                pass
            finally:
                bak_tmp.unlink(missing_ok=True)
        for attempt in range(10):
            try:
                os.replace(temp, path)
                break
            except PermissionError:  # Windows: читатель держит файл долю секунды
                if attempt == 9:
                    raise
                time.sleep(0.02)
    finally:
        temp.unlink(missing_ok=True)


def _read_json_src(path: Path, default: Callable[[], dict]) -> tuple[dict, bool]:
    """Читает JSON-объект. Второе значение True, если файл битый/нечитаемый и взята
    копия .bak или пустой дефолт. Битый файл откладывается в сторону, а не удаляется."""
    exists = path.is_file()
    for candidate in (path, path.with_name(path.name + ".bak")):
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            return data, candidate != path
        if candidate == path:
            with contextlib.suppress(OSError):
                os.replace(path, path.with_name(f"{path.name}.corrupt-{int(time.time())}"))
    return default(), exists


def _read_json(path: Path, default: Callable[[], dict]) -> dict:
    return _read_json_src(path, default)[0]


def _file_schema(data: dict, path: Path) -> bool:
    """Проверяет schema_version. Возвращает True, если поле отсутствует (считаем 1, надо дописать)."""
    if "schema_version" not in data:
        return True
    ver = data["schema_version"]
    if isinstance(ver, bool) or not isinstance(ver, int) or ver < 1:
        raise RulesStoreError(f"{path.name}: invalid schema_version {ver!r}; file left untouched")
    if ver > SCHEMA_VERSION:
        raise SchemaTooNew(
            f"{path.name} has schema_version {ver}, this build supports up to {SCHEMA_VERSION}; "
            "file left untouched - update the app")
    return False


def _repair_tail(path: Path) -> bool:
    """Если файл кончается не на перевод строки: валидный хвост дополняем, битый обрезаем.

    Возвращает True, если пришлось что-то чинить. Вызывать под блокировкой.
    """
    if not path.is_file() or path.stat().st_size == 0:
        return False
    with open(path, "rb+") as fh:
        fh.seek(-1, os.SEEK_END)
        if fh.read(1) == b"\n":
            return False
        fh.seek(0)
        raw = fh.read()
        cut = raw.rfind(b"\n") + 1
        tail = raw[cut:]
        try:
            json.loads(tail.decode("utf-8"))
            fh.seek(0, os.SEEK_END)
            fh.write(b"\n")  # запись целая, не хватало только конца строки
        except (ValueError, UnicodeDecodeError):
            fh.truncate(cut)
        fh.flush()
        os.fsync(fh.fileno())
    return True


def _append_jsonl(path: Path, record: dict) -> None:
    line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as fh:
        fh.write(line.encode("utf-8"))
        fh.flush()
        os.fsync(fh.fileno())


def _read_jsonl(path: Path) -> list[dict]:
    """Все целые записи; битые строки (оборванная запись) пропускаются."""
    if not path.is_file():
        return []
    out: list[dict] = []
    with open(path, "rb") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def _merge(base: Any, changes: Any) -> Any:
    """Глубокое слияние словарей; списки и скаляры заменяются целиком."""
    if isinstance(base, dict) and isinstance(changes, dict):
        out = dict(base)
        for k, v in changes.items():
            out[k] = _merge(base.get(k), v) if k in base else copy.deepcopy(v)
        return out
    return copy.deepcopy(changes)


# --- хранилище ----------------------------------------------------------------

class RulesStore:
    def __init__(self, directory: str | os.PathLike | None = None,
                 now: Callable[[], str] = _utc_now):
        self._dir = Path(directory) if directory else None
        self._now = now
        self.problems: list[dict] = []   # битые правила с последнего list_rules

    @property
    def directory(self) -> Path:
        # каталог разрешается при каждом обращении: ACL_CONFIG_DIR могут сменить в тестах
        return self._dir if self._dir else Path(get_local_config_dir())

    def _p(self, name: str) -> Path:
        return self.directory / name

    def lock(self):
        return _file_lock(self._p(LOCK_FILE))

    # -- правила --

    def _read_rules(self) -> tuple[dict, bool, bool]:
        path = self._p(RULES_FILE)
        existed = path.is_file()
        data, fallback = _read_json_src(path, lambda: {})
        unversioned = _file_schema(data, path)
        rules = data.get("rules")
        out = {"schema_version": SCHEMA_VERSION, "rules": rules if isinstance(rules, dict) else {}}
        # файл без версии (или восстановленный из копии) надо перезаписать с версией
        return out, fallback, unversioned and existed and not fallback and bool(data)

    @staticmethod
    def rule_problem(rule: Any) -> str | None:
        """Почему правило нельзя использовать (None - всё в порядке)."""
        if not isinstance(rule, dict):
            return "rule is not an object"
        versions = rule.get("versions")
        if not isinstance(versions, list) or not versions or \
                not all(isinstance(v, dict) and "version" in v for v in versions):
            return "missing or invalid versions"
        if not any(v["version"] == rule.get("current_version") for v in versions):
            return f"current_version {rule.get('current_version')!r} not in versions"
        if not isinstance(rule.get("id"), str):
            return "missing id"
        return None

    def _normalise(self, data: dict, restored: bool) -> list[dict]:
        """Automatic без действующего гранта - это Suggest. После восстановления из
        копии все гранты снимаются: старая копия не должна заново включать автоматику."""
        notes: list[dict] = []
        for rule in data["rules"].values():
            if self.rule_problem(rule):
                continue
            if restored and (rule.get("grant") or rule.get("mode") == "automatic"):
                reason = "restored_from_backup"
            elif rule.get("mode") == "automatic" and not automatic_available():
                reason = "automatic is not available"
            elif rule.get("mode") == "automatic" and self.active_grant(rule) is None:
                reason = "automatic without a valid grant on load"
            else:
                continue
            self._drop_grant(rule, reason)
            notes.append({"kind": "grant_dropped", "state": "system", "rule_id": rule.get("id"),
                          "reason": reason})
        return notes

    def _load(self) -> dict:
        data, fallback, unversioned = self._read_rules()
        if not fallback and not unversioned and not any(
                not self.rule_problem(r) and r.get("mode") == "automatic"
                and (not automatic_available() or self.active_grant(r) is None)
                for r in data["rules"].values()):
            return data
        with self.lock():
            data, fallback, unversioned = self._read_rules()
            notes = self._normalise(data, fallback)
            if notes or fallback or unversioned:
                self._save(data)
                for note in notes:
                    self._append(ACTIONS_FILE, note)
        return data

    def _save(self, data: dict) -> None:
        _atomic_write_json(self._p(RULES_FILE), {**data, "schema_version": SCHEMA_VERSION})

    def get_rule(self, rule_id: str) -> dict:
        rule = self._load()["rules"].get(rule_id)
        if rule is None:
            raise RuleNotFound(rule_id)
        problem = self.rule_problem(rule)
        if problem:
            raise RulesStoreError(f"rule {rule_id} is malformed: {problem}")
        return rule

    def list_rules_checked(self, owner_cm: str | None = None) -> tuple[list[dict], list[dict]]:
        """(правила, проблемы): битое правило пропускается и попадает в problems, остальные живут.
        Файл при этом не трогаем - битое правило остаётся на диске для ручного разбора."""
        rules: list[dict] = []
        problems: list[dict] = []
        for key, rule in self._load()["rules"].items():
            problem = self.rule_problem(rule)
            if problem:
                rid = rule.get("id", key) if isinstance(rule, dict) else key
                log.warning("rules: malformed rule %s skipped: %s", rid, problem)
                problems.append({"rule_id": rid, "error": problem})
            elif owner_cm is None or rule.get("owner_cm") == owner_cm:
                rules.append(rule)
        return sorted(rules, key=lambda r: (r.get("created_at") or "", r["id"])), problems

    def list_rules(self, owner_cm: str | None = None) -> list[dict]:
        rules, self.problems = self.list_rules_checked(owner_cm)
        return rules

    @staticmethod
    def current_version(rule: dict) -> dict:
        problem = RulesStore.rule_problem(rule)
        if problem:
            rid = rule.get("id") if isinstance(rule, dict) else None
            raise RulesStoreError(f"rule {rid} is malformed: {problem}")
        for v in rule["versions"]:
            if v["version"] == rule["current_version"]:
                return v
        raise RulesStoreError(f"rule {rule['id']} has no version {rule['current_version']}")

    @staticmethod
    def active_grant(rule: dict) -> dict | None:
        """Грант, который действует сейчас: режим Automatic и хеши совпадают с текущей версией."""
        grant = rule.get("grant")
        if rule.get("mode") != "automatic" or not grant:
            return None
        cur = RulesStore.current_version(rule)
        want = model.grant_hashes(cur)
        return grant if all(grant.get(k) == v for k, v in want.items()) else None

    def create_rule(self, owner_cm: str, fields: dict, *, mode: str = "observe",
                    by: str = "") -> dict:
        """Новое правило, версия 1. Automatic выдаётся только через grant_automatic."""
        if mode not in model.MODES or mode == "automatic":
            raise model.RuleValidationError(["new rule mode must be disabled, observe or suggest"])
        if not str(owner_cm or "").strip():
            raise model.RuleValidationError(["owner_cm is required"])
        version = model.make_version(**fields)
        model.check_version(version)
        now = self._now()
        rule_id = "rule_" + uuid.uuid4().hex[:12]
        rule = {
            "id": rule_id, "owner_cm": owner_cm.strip(), "created_at": now,
            "current_version": 1, "mode": mode, "grant": None,
            "versions": [{**version, "version": 1, "created_at": now, "created_by": by}],
        }
        with self.lock():
            data = self._load()
            data["rules"][rule_id] = rule
            self._save(data)
        return rule

    def create_from_preset(self, preset_id: str, owner_cm: str, *, by: str = "",
                           **overrides: Any) -> dict:
        fields, mode = model.preset(preset_id, **overrides)
        return self.create_rule(owner_cm, fields, mode=mode, by=by)

    def edit_rule(self, rule_id: str, changes: dict, *, by: str = "",
                  expected_version: int | None = None) -> dict:
        """Правка = новая версия. Если менялись scope/conditions/action/caps, грант аннулируется,
        а Automatic-правило уходит в Suggest. Правка без изменений версию не создаёт."""
        with self.lock():
            data = self._load()
            rule = data["rules"].get(rule_id)
            if rule is None:
                raise RuleNotFound(rule_id)
            if expected_version is not None and expected_version != rule["current_version"]:
                raise VersionConflict(f"{rule_id}: current v{rule['current_version']}, expected v{expected_version}")
            cur = self.current_version(rule)
            meta = ("version", "created_at", "created_by")
            body = {k: v for k, v in cur.items() if k not in meta}
            new_body = _merge(body, {k: v for k, v in changes.items() if k not in meta})
            model.check_version(new_body)
            if new_body == body:
                return rule
            number = max(v["version"] for v in rule["versions"]) + 1
            rule["versions"].append({**new_body, "version": number,
                                     "created_at": self._now(), "created_by": by})
            rule["current_version"] = number
            self._revoke_if_stale(rule, reason="edit")
            self._save(data)
            return rule

    def _revoke_if_stale(self, rule: dict, *, reason: str) -> None:
        grant = rule.get("grant")
        if not grant:
            return
        want = model.grant_hashes(self.current_version(rule))
        if all(grant.get(k) == v for k, v in want.items()):
            return
        self._drop_grant(rule, reason)

    def _drop_grant(self, rule: dict, reason: str) -> None:
        rule["grant"] = None
        rule["grant_revoked"] = {"at": self._now(), "reason": reason,
                                 "version": rule["current_version"]}
        if rule.get("mode") == "automatic":
            rule["mode"] = "suggest"

    def set_mode(self, rule_id: str, mode: str, *, by: str = "") -> dict:
        """Смена режима. В Automatic - только через grant_automatic; выход из него снимает грант."""
        if mode not in model.MODES:
            raise model.RuleValidationError([f"mode must be one of {model.MODES}"])
        if mode == "automatic":
            raise model.RuleValidationError(["Automatic is granted with grant_automatic, not set_mode"])
        with self.lock():
            data = self._load()
            rule = data["rules"].get(rule_id)
            if rule is None:
                raise RuleNotFound(rule_id)
            if rule["mode"] == "automatic" and rule.get("grant"):
                self._drop_grant(rule, f"mode changed to {mode} by {by}")
            rule["mode"] = mode
            self._save(data)
            return rule

    def grant_automatic(self, rule_id: str, *, by: str) -> dict:
        """Явный opt-in Automatic для ТЕКУЩЕЙ версии (дизайн 5.8)."""
        if not automatic_available():
            raise model.RuleValidationError([AUTOMATIC_UNAVAILABLE_MESSAGE])
        if not str(by or "").strip():
            raise model.RuleValidationError(["grant needs a name of who grants it"])
        with self.lock():
            data = self._load()
            rule = data["rules"].get(rule_id)
            if rule is None:
                raise RuleNotFound(rule_id)
            cur = self.current_version(rule)
            blockers = model.automatic_blockers(cur)
            if blockers:
                raise model.RuleValidationError(blockers)
            rule["grant"] = {"granted_at": self._now(), "by": by.strip(),
                             "version": cur["version"], **model.grant_hashes(cur)}
            rule["grant_revoked"] = None
            rule["mode"] = "automatic"
            self._save(data)
            return rule

    def revoke_grant(self, rule_id: str, *, by: str = "") -> dict:
        """«Stop automation»: грант аннулируется, правило уходит в Suggest."""
        with self.lock():
            data = self._load()
            rule = data["rules"].get(rule_id)
            if rule is None:
                raise RuleNotFound(rule_id)
            self._drop_grant(rule, f"revoked by {by}")
            self._save(data)
            return rule

    def duplicate(self, rule_id: str, *, by: str = "", name: str | None = None) -> dict:
        """Копия: Disabled, без гранта, версия 1 с содержимым текущей версии."""
        with self.lock():
            data = self._load()
            src = data["rules"].get(rule_id)
            if src is None:
                raise RuleNotFound(rule_id)
            cur = self.current_version(src)
            body = {k: copy.deepcopy(v) for k, v in cur.items()
                    if k not in ("version", "created_at", "created_by")}
            body["name"] = name or f"{cur['name']} (copy)"
            model.check_version(body)
            now = self._now()
            new_id = "rule_" + uuid.uuid4().hex[:12]
            data["rules"][new_id] = {
                "id": new_id, "owner_cm": src["owner_cm"], "created_at": now,
                "current_version": 1, "mode": "disabled", "grant": None,
                "copied_from": {"rule_id": rule_id, "version": cur["version"]},
                "versions": [{**body, "version": 1, "created_at": now, "created_by": by}],
            }
            self._save(data)
            return data["rules"][new_id]

    # -- настройки и kill switch --

    def load_settings(self) -> dict:
        path = self._p(SETTINGS_FILE)
        existed = path.is_file()
        raw, fallback = _read_json_src(path, lambda: {})
        unversioned = _file_schema(raw, path)
        if unversioned and existed and raw and not fallback:
            with self.lock():  # файл без версии: считаем 1 и дописываем
                _atomic_write_json(path, {**raw, "schema_version": SCHEMA_VERSION})
        if fallback:
            # копия .bak отстаёт на одну запись и могла бы включить автоматику заново
            raw = dict(raw, automatic_enabled=False)
            with self.lock():
                _atomic_write_json(self._p(SETTINGS_FILE),
                                   {**raw, "schema_version": SCHEMA_VERSION, "changed_at": self._now()})
        out = copy.deepcopy(DEFAULT_SETTINGS)
        for k, v in raw.items():
            out[k] = _merge(out.get(k), v) if k in out else v
        out["schema_version"] = SCHEMA_VERSION
        # любое нечитаемое значение флага = выключено; пока замок Automatic закрыт - тоже
        if out.get("automatic_enabled") is not True or not automatic_available():
            out["automatic_enabled"] = False
        return out

    def update_settings(self, changes: dict, *, by: str = "") -> dict:
        if changes.get("automatic_enabled") is True and not automatic_available():
            raise RulesStoreError(AUTOMATIC_UNAVAILABLE_MESSAGE)
        with self.lock():
            settings = self.load_settings()
            settings = _merge(settings, changes)
            settings["changed_at"] = self._now()
            settings["changed_by"] = by
            _atomic_write_json(self._p(SETTINGS_FILE), settings)
            return settings

    def set_automatic_enabled(self, enabled: bool, *, by: str = "") -> dict:
        if enabled and not automatic_available():
            raise RulesStoreError(AUTOMATIC_UNAVAILABLE_MESSAGE)
        return self.update_settings({"automatic_enabled": bool(enabled)}, by=by)

    # -- журналы --

    def _append(self, name: str, record: dict) -> dict:
        rec = {"at": self._now(), **record}
        with self.lock():
            path = self._p(name)
            _repair_tail(path)
            _append_jsonl(path, rec)
        return rec

    def append_evaluation(self, record: dict) -> dict:
        return self._append(EVALUATIONS_FILE, {"evaluation_id": "ev_" + uuid.uuid4().hex[:12], **record})

    def append_action(self, record: dict) -> dict:
        """Строка на каждый переход состояния действия (последняя запись по action_id истинна)."""
        return self._append(ACTIONS_FILE, {"action_id": "act_" + uuid.uuid4().hex[:12], **record})

    def add_hold(self, object_id: str, reason: str) -> dict:
        return self._append(HOLDS_FILE, {"object_id": object_id, "reason": reason,
                                         "released_at": None, "released_by": None})

    def release_hold(self, object_id: str, *, by: str) -> dict:
        return self._append(HOLDS_FILE, {"object_id": object_id, "reason": "release",
                                         "released_at": self._now(), "released_by": by})

    def evaluations(self) -> list[dict]:
        return _read_jsonl(self._p(EVALUATIONS_FILE))

    def actions(self) -> list[dict]:
        return _read_jsonl(self._p(ACTIONS_FILE))

    def holds(self) -> list[dict]:
        return _read_jsonl(self._p(HOLDS_FILE))

    def active_holds(self) -> dict[str, dict]:
        """object_id -> запись о hold, который ещё не снят человеком."""
        out: dict[str, dict] = {}
        for rec in self.holds():
            oid = rec.get("object_id")
            if not oid:
                continue
            if rec.get("released_at"):
                out.pop(oid, None)
            else:
                out[oid] = rec
        return out

    def latest_actions(self) -> dict[str, dict]:
        """action_id -> последнее состояние."""
        out: dict[str, dict] = {}
        for rec in self.actions():
            if rec.get("action_id"):
                out[rec["action_id"]] = rec
        return out

    def last_action_for(self, object_id: str) -> dict | None:
        """Последнее действие по объекту (для cooldown, который живёт на диске)."""
        found = None
        for rec in self.actions():
            if rec.get("id") == object_id:
                found = rec
        return found
