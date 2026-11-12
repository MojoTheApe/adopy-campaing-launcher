"""ACL-1856: журнал операции создания кампании - что уже ушло в Meta и что подтверждено.

Один JSON-файл на операцию: `<get_local_config_dir()>/api_operations/<operation_id>.json`. Та же схема хранения,
что у `name_receipt` и `creative_recovery.Receipt` (замок и атомарная запись из `config.paths`).

Файл говорит только, ЧТО проверить: id и состояние этапов. Верить ему без чтения Meta нельзя - создатель
сверяет каждый подтверждённый этап чтением перед первой записью запуска. Модуль не ходит в Meta и не пишет
ничего, кроме своего файла; чтение (`read`, `summary`) файл не меняет.

Состояния этапа идут только вперёд: not_sent -> sent -> confirmed | unknown | rejected | cancelled.
`confirmed` назад не возвращается. Повторная попытка после unknown/rejected/cancelled снова ставит `sent`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from config.paths import get_local_config_dir, machine_file_lock, _atomic_write

log = logging.getLogger(__name__)

SCHEMA = 1
#: Сколько после неизвестного результата записи мы не повторяем её вслепую (спека §7, решение 2).
UNKNOWN_HOLD_S = 10 * 60
#: How long any operation file is kept after its last change (complete, open, stopped or failed alike), and the side
#: files (.corrupt, .superseded, .v1.json, orphan locks) after their own last change.
RETENTION_S = 30 * 24 * 3600
#: The folder is scanned for old files at most this often (the scan reads files; `open` runs for every campaign).
PRUNE_EVERY_S = 6 * 3600
#: A request id can only collide with a recent operation: the duplicate scan reads this many newest files at most.
REQUEST_SCAN_MAX = 200
REQUEST_SCAN_AGE_S = 7 * 24 * 3600
#: Stage note of an unknown write that was looked for by readback and not found (the next run sends it again).
NOT_FOUND_NOTE = "not_found"

NOT_SENT, SENT, CONFIRMED, REJECTED, CANCELLED, UNKNOWN = (
    "not_sent", "sent", "confirmed", "rejected", "cancelled", "unknown")
STAGE_STATES = (NOT_SENT, SENT, CONFIRMED, REJECTED, CANCELLED, UNKNOWN)

#: Операция: open - идёт или оборвана; complete - всё создано и сверено; stopped - остановлена с понятной причиной;
#: failed - неожиданный сбой (catch-all создателя).
OP_OPEN, OP_COMPLETE, OP_STOPPED, OP_FAILED = "open", "complete", "stopped", "failed"
_FINISHED = (OP_COMPLETE,)

#: Куда из какого состояния можно перейти (назад - нельзя; sent -> not_sent только для запрещённой политикой записи,
#: которая в Meta не уходила).
_ALLOWED = {
    NOT_SENT: {SENT},
    SENT: {CONFIRMED, UNKNOWN, REJECTED, CANCELLED, NOT_SENT, SENT},
    UNKNOWN: {SENT, CONFIRMED, REJECTED, CANCELLED},
    REJECTED: {SENT},
    CANCELLED: {SENT},
    CONFIRMED: set(),
}


class RequestConflict(ValueError):
    """The same request id was already used for a different plan revision (another operation)."""


class JournalError(Exception):
    """Журнал нельзя прочитать или записать (диск полон, нет прав, замок): запись в Meta не уходит."""


class StageBusy(JournalError):
    """Another run (this or another process) sent this stage a moment ago: it is not sent again from here."""


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def plan_fingerprint(plan_view: dict, file_md5: dict[str, Any], slot: int = 0) -> str:
    """Отпечаток плана: канонический JSON плана (без `warnings`) + md5 файлов каждого объявления + место черновика.

    The plan is the one SUBMITTED (before name choice and start-time moves), so the same request has the same
    fingerprint whatever Meta holds and whatever the clock says. `slot` is the draft position in its submit:
    two identical drafts of one submit are two operations.

    Без временных меток и абсолютных путей: вызывающий отдаёт имена файлов, а не пути кэша сжатого видео.
    Изменённый файл или план = другой отпечаток = новая операция.
    """
    view = {k: v for k, v in plan_view.items() if k != "warnings"}
    return hashlib.sha256(_canon({"plan": view, "files": file_md5, "slot": int(slot)}).encode("utf-8")).hexdigest()


def operation_id(act: str, tag: str, fingerprint: str) -> str:
    raw = "|".join((str(act), str(tag), str(fingerprint)))
    return "op-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _fresh_stage(kind: str, name: str, content_md5: str) -> dict:
    return {"kind": kind, "state": NOT_SENT, "object_id": "", "name": name, "content_md5": content_md5,
            "sent_at": 0.0, "confirmed_at": 0.0, "note": "", "attempts": 0}


class Operation:
    """Одна операция. Каждое изменение: замок -> перечитать файл -> применить -> атомарно записать."""

    def __init__(self, journal: "OperationJournal", data: dict, path: Path):
        self._journal = journal
        self.path = path
        self.data = data
        #: Что случилось с файлом при открытии (повреждён, неизвестная версия): в предупреждения запуска.
        self.warnings: list[str] = []
        #: Owner token of THIS run (process + random): stamped on every stage it sends, checked under the file lock.
        self.owner = f"{os.getpid()}-{uuid.uuid4().hex[:10]}"

    @property
    def id(self) -> str:
        return self.data["operation_id"]

    @property
    def state(self) -> str:
        return self.data["state"]

    def stage(self, key: str) -> dict | None:
        st = self.data["stages"].get(key)
        return dict(st) if st else None

    def campaign_id(self) -> str:
        """Id of the confirmed campaign of this operation ('' when none)."""
        st = self.data["stages"].get("campaign") or {}
        return str(st.get("object_id") or "") if st.get("state") == CONFIRMED else ""

    @property
    def published_name(self) -> str:
        return str(self.data.get("published_name") or "")

    def refresh(self) -> None:
        """Re-read the file (another process may have moved a stage). Unreadable: keep the copy in memory."""
        try:
            disk = self._journal._read_file(self.path)
        except JournalError:
            return
        if disk is not None:
            self.data = disk

    def confirmed_stages(self) -> list[tuple[str, dict]]:
        """Подтверждённые этапы с id, в порядке записи (для сверки перед возобновлением)."""
        return [(k, dict(self.data["stages"][k])) for k in self.data["order"]
                if self.data["stages"][k]["state"] == CONFIRMED and self.data["stages"][k]["object_id"]]

    # -- изменения
    def _mutate(self, fn: Callable[[dict], None]) -> None:
        j = self._journal
        try:
            with machine_file_lock(self.path.with_suffix(".lock")):
                disk = j._read_file(self.path)
                data = disk if disk is not None else self.data
                fn(data)
                data["updated_at"] = j.clock()
                _atomic_write(self.path, _canon(data).encode("utf-8"))
                self.data = data
        except JournalError:
            raise
        except (OSError, ValueError, TimeoutError) as exc:
            raise JournalError(f"operation journal cannot be written: {exc}") from exc

    def begin(self, key: str, kind: str, *, name: str = "", content_md5: str = "", supersede: bool = False) -> None:
        """Записать `sent` ДО отправки. Подтверждённый этап заново не отправляется - ошибка.

        `supersede` - только креатив, заменяющий устаревший (тот же ключ, другое содержимое): старый id уходит в note.
        """
        def apply(d: dict) -> None:
            st = d["stages"].get(key)
            if st is None:
                st = _fresh_stage(kind, name, content_md5)
                d["stages"][key] = st
                d["order"].append(key)
            elif st["state"] == CONFIRMED:
                if not supersede:
                    raise JournalError(f"stage {key!r} is already confirmed")
                st.update(state=NOT_SENT, note=f"replaced {st['object_id']}", object_id="")
            elif SENT not in _ALLOWED[st["state"]]:
                raise JournalError(f"stage {key!r}: {st['state']} -> sent is not allowed")
            if (st["state"] == SENT and st.get("owner") and st["owner"] != self.owner
                    and self._journal.clock() - float(st.get("sent_at") or 0.0) < UNKNOWN_HOLD_S):
                # Decided under the file lock on the CURRENT file: two processes cannot both send the same step.
                raise StageBusy(f"stage {key!r} was sent by another run a moment ago")
            st.update(state=SENT, sent_at=self._journal.clock(), note="", attempts=int(st.get("attempts", 0)) + 1,
                      owner=self.owner)
            if name:
                st["name"] = name
            if content_md5:
                st["content_md5"] = content_md5
        self._mutate(apply)

    def confirm(self, key: str, object_id: str) -> None:
        def apply(d: dict) -> None:
            st = d["stages"].get(key)
            if st is None:
                raise JournalError(f"stage {key!r} was never sent")
            if st["state"] == CONFIRMED:
                if st["object_id"] != str(object_id):
                    raise JournalError(f"stage {key!r} is confirmed with another id")
                return
            if CONFIRMED not in _ALLOWED[st["state"]]:
                raise JournalError(f"stage {key!r}: {st['state']} -> confirmed is not allowed")
            st.update(state=CONFIRMED, object_id=str(object_id), confirmed_at=self._journal.clock(),
                      owner=self.owner)
        self._mutate(apply)

    def mark(self, key: str, state: str, note: str = "") -> None:
        if state not in STAGE_STATES:
            raise ValueError(state)

        def apply(d: dict) -> None:
            st = d["stages"].get(key)
            if st is None:
                raise JournalError(f"stage {key!r} was never sent")
            if st["state"] == state:
                return
            if state not in _ALLOWED[st["state"]]:
                raise JournalError(f"stage {key!r}: {st['state']} -> {state} is not allowed")
            st.update(state=state, note=note)
        self._mutate(apply)

    def add_request_id(self, request_id: str) -> None:
        if not request_id:
            return

        def apply(d: dict) -> None:
            if request_id not in d["request_ids"]:
                d["request_ids"].append(request_id)
        self._mutate(apply)

    def set_published(self, name: str, copy_number: int = 0) -> None:
        """The campaign name this operation publishes under (after name choice); the identity is the SUBMITTED plan."""
        def apply(d: dict) -> None:
            d["published_name"] = str(name)
            d["copy_number"] = int(copy_number)
        if self.published_name != str(name) or int(self.data.get("copy_number") or 0) != int(copy_number):
            self._mutate(apply)

    def finish(self, state: str, note: str = "") -> None:
        """Итог операции: complete / stopped / failed. A run that stops decides the state even after an earlier
        complete one: the summary and the next action must agree with the error of the run that just stopped."""
        def apply(d: dict) -> None:
            d["state"] = state
            d["note"] = note
            if state == OP_COMPLETE:
                d["completed_at"] = self._journal.clock()
        self._mutate(apply)

    # -- чтение
    def age_s(self, key: str) -> float:
        st = self.data["stages"].get(key) or {}
        return max(0.0, self._journal.clock() - float(st.get("sent_at") or 0.0))

    def partial_ids(self) -> dict[str, str]:
        return {k: s["object_id"] for k, s in self.data["stages"].items()
                if s["state"] == CONFIRMED and s["object_id"]}

    def next_action(self) -> str:
        """One plain next step for the manager (English). Reads only."""
        st = self.data["stages"]
        if self.state == OP_COMPLETE:
            return "Nothing to do - everything was created and checked (campaign is PAUSED)."
        note = str(self.data.get("note") or "")
        if note == "resume_mismatch":
            return ("Check the object named in the error in Ads Manager (fix it or delete it there), "
                    "then publish again.")
        if note == "journal":
            return "Free some disk space or check the folder permissions, then publish again."
        # Only a write whose outcome is really unknown waits; one that was looked for and not found is sent again.
        unknown = [k for k, s_ in st.items() if s_["state"] == UNKNOWN and s_.get("note") != NOT_FOUND_NOTE]
        if unknown or any(s_["state"] == SENT for s_ in st.values()):
            return "Wait 10 minutes, then publish again - Meta may still be processing the last step."
        if any(s_["state"] == CONFIRMED for s_ in st.values()) or self.state in (OP_STOPPED, OP_FAILED):
            return "Publish again - what was already created is checked and nothing will be created twice."
        return "Publish again."

    def summary(self) -> dict:
        """Для результата задания: состояние, этапы, подтверждённые id и следующий шаг. Файл не меняет."""
        return {"id": self.id, "state": self.state,
                "stages": [{"label": k, "state": self.data["stages"][k]["state"],
                            "id": self.data["stages"][k]["object_id"]} for k in self.data["order"]],
                "partial_ids": self.partial_ids(), "next_action": self.next_action()}


class OperationJournal:
    """Папка с файлами операций. `root=None` - машинная папка ACL; `clock` подменяется в тестах."""

    def __init__(self, root: Path | str | None = None, *, clock: Callable[[], float] = time.time):
        self._root = Path(root) if root is not None else None
        self.clock = clock

    @property
    def root(self) -> Path:
        return self._root if self._root is not None else Path(get_local_config_dir()) / "api_operations"

    def path_for(self, op_id: str) -> Path:
        return self.root / f"{op_id}.json"

    def _read_file(self, path: Path) -> dict | None:
        """Файл операции или None. Повреждённый - JournalError; чужая версия - JournalError с пометкой."""
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise JournalError(f"operation journal cannot be read: {exc}") from exc
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise JournalError(f"operation journal is corrupt: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("stages"), dict) \
                or not isinstance(data.get("order"), list):
            raise JournalError("operation journal is corrupt: unexpected content")
        if data.get("v") != SCHEMA:
            raise JournalError(f"operation journal has an unknown version: {data.get('v')!r}")
        return data

    def read(self, op_id: str) -> dict | None:
        """Только чтение (GET статуса/истории): файл не создаётся и не меняется. Нет файла или он негодный - None."""
        try:
            return self._read_file(self.path_for(op_id))
        except JournalError:
            return None

    def open(self, act: str, tag: str, fingerprint: str, name: str, *, request_id: str = "") -> Operation:
        """Открыть (или создать) операцию. Тот же отпечаток = тот же файл.

        Повреждённый файл уезжает в `.corrupt`, операция начинается заново (с предупреждением).
        Файл с неизвестной версией не трогаем и не используем: операция начинается заново рядом, под другим именем.
        """
        op_id = operation_id(act, tag, fingerprint)
        path = self.path_for(op_id)
        warnings: list[str] = []
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with machine_file_lock(path.with_suffix(".lock")):
                try:
                    data = self._read_file(path)
                except JournalError as exc:
                    if "unknown version" in str(exc):
                        # Файл новее нашего кода: не портим; начинаем заново в файле с суффиксом версии.
                        warnings.append("An operation record from a newer version was ignored.")
                        path = path.with_name(f"{op_id}.v{SCHEMA}.json")
                        data = self._read_file(path) if path.exists() else None
                    else:
                        try:
                            # Timestamped: an earlier .corrupt file of the same operation is never overwritten.
                            path.replace(path.with_name(f"{op_id}.{int(self.clock())}.corrupt"))
                        except OSError:
                            pass
                        warnings.append("The saved progress of this campaign was unreadable and was set aside; "
                                        "the campaign will be checked in Ads Manager by name.")
                        data = None
                if request_id:
                    owner = self._request_owner(request_id, skip=path)
                    if owner and owner != op_id:
                        raise RequestConflict("This request ID was used for a different campaign plan.")
                if data is None:
                    now = self.clock()
                    data = {"v": SCHEMA, "operation_id": op_id, "act": str(act), "tag": str(tag),
                            "name": str(name), "fingerprint": fingerprint, "state": OP_OPEN, "note": "",
                            "created_at": now, "updated_at": now, "request_ids": [],
                            "stages": {}, "order": []}
                    _atomic_write(path, _canon(data).encode("utf-8"))
            self.prune()
        except RequestConflict:
            raise
        except (OSError, ValueError, TimeoutError) as exc:
            raise JournalError(f"operation journal cannot be opened: {exc}") from exc
        op = Operation(self, data, path)
        op.warnings = warnings
        if request_id:
            op.add_request_id(request_id)
        return op

    def _request_owner(self, request_id: str, skip: Path | None = None) -> str:
        """Operation id that already holds this request id ('' if none). Read-only, capped scan of recent files:
        a submit key can only collide with a recent operation, and `open` runs for every campaign."""
        try:
            stamped = []
            for f in self.root.glob("op-*.json"):
                try:
                    stamped.append((f.stat().st_mtime, f))
                except OSError:
                    continue
        except OSError:
            return ""
        stamped.sort(key=lambda t: t[0], reverse=True)
        horizon = time.time() - REQUEST_SCAN_AGE_S
        for mtime, f in stamped[:REQUEST_SCAN_MAX]:
            if mtime < horizon:
                break
            try:
                d = self._read_file(f)
            except JournalError:
                continue
            if d and request_id in (d.get("request_ids") or []):
                return str(d.get("operation_id") or "")
        return ""

    def supersede(self, op: Operation, *, request_id: str = "") -> Operation:
        """Set the earlier record aside (`.superseded` file, kept for the retention period) and open a fresh operation
        with the same identity: used when the campaign it describes is gone from Ads Manager or was replaced."""
        d = op.data
        try:
            with machine_file_lock(op.path.with_suffix(".lock")):
                if op.path.exists():
                    op.path.replace(op.path.with_name(f"{op.id}.{int(self.clock())}.superseded"))
        except (OSError, TimeoutError) as exc:
            raise JournalError(f"operation journal cannot be updated: {exc}") from exc
        return self.open(d["act"], d["tag"], d["fingerprint"], d.get("name", ""), request_id=request_id)

    def _prune_due(self) -> bool:
        marker = self.root / ".last_prune"
        now = self.clock()
        try:
            last = float(marker.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            last = 0.0
        if 0 <= now - last < PRUNE_EVERY_S:
            return False
        try:
            marker.write_text(str(now), encoding="utf-8")
        except OSError:
            pass
        return True

    def prune(self, *, force: bool = False) -> int:
        """Delete operation files, and the side files, that were last touched longer ago than the retention period.

        Any state (complete, open, stopped, failed) goes after `RETENTION_S`: the name/tag lookup of the next run
        does not need the file. At most one scan per `PRUNE_EVERY_S`. Errors are never raised.
        """
        if not force and not self._prune_due():
            return 0
        removed = 0
        try:
            now = self.clock()
            for path in list(self.root.glob("op-*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(data, dict):
                    continue
                last = float(data.get("completed_at") or data.get("updated_at") or now) \
                    if data.get("state") in _FINISHED else float(data.get("updated_at") or now)
                if now - last > RETENTION_S:
                    path.unlink(missing_ok=True)
                    path.with_suffix(".lock").unlink(missing_ok=True)
                    removed += 1
            # Side files have no state: their own age (file time) decides.
            real = time.time()
            for pattern in ("op-*.corrupt", "op-*.superseded", "op-*.v1.json", "op-*.lock"):
                for path in list(self.root.glob(pattern)):
                    try:
                        if real - path.stat().st_mtime > RETENTION_S and (
                                path.suffix != ".lock" or not path.with_suffix(".json").exists()):
                            path.unlink(missing_ok=True)
                            removed += 1
                    except OSError:
                        continue
        except OSError:
            log.debug("operation journal prune failed", exc_info=True)
        return removed
