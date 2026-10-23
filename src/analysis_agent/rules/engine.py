"""Rules T3 (ACL-1816): движок оценки - триггеры, один рабочий поток, журналы.

Дизайн §4.1: оценка запускается ТОЛЬКО по событию.

* `workbook` - собрана новая книга BI (ключ кэша отчёта сменился);
* `manual` - человек нажал «Evaluate now»;
* `snapshot` - человек нажал Read campaigns и чтение закончилось.

Никаких таймеров и опроса: один поток блокируется на очереди и спит, пока не
придёт задание. Одинаковые задания, ещё не начатые, схлопываются в одно. Движок
не знает про Meta, профили и сессии: данные ему даёт `Sources` (локальные чтения),
запись - только в журналы правил (`rule_evaluations.jsonl`, `rule_actions.jsonl`).
Предложение (Suggest) - строка `state=proposed` в журнале действий; повторная оценка
той же книги с тем же снимком новых строк не создаёт: ключ - версия правила + объект
+ вид действия + отпечаток доказательств.
"""
from __future__ import annotations

import logging
import queue
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from . import arbiter, evaluate as ev_mod, gather as gather_mod
from .store import RulesStore

log = logging.getLogger(__name__)

TRIGGERS = ("workbook", "manual", "snapshot")
_STOP = object()


def dedup_key(rule_id: str, version: int, obj_key: str, kind: str, fingerprint: str) -> str:
    return "|".join((rule_id, str(version), obj_key, kind, fingerprint))


class RulesEngine:
    def __init__(self, store: RulesStore, sources: gather_mod.Sources, *,
                 now: Callable[[], datetime] | None = None):
        self.store = store
        self.sources = sources
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._queue: queue.Queue = queue.Queue()
        self._pending: dict[tuple, dict] = {}
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._busy = False
        self.problems: list[dict] = []
        self._thread: threading.Thread | None = None

    # -- очередь и поток --

    def enqueue(self, trigger: str, scope: str | None = None) -> bool:
        """Поставить оценку в очередь. `False`, если такая уже ждёт (схлопнуто). Ничего не оценивает."""
        if trigger not in TRIGGERS:
            raise ValueError(f"unknown trigger {trigger!r}")
        job = {"trigger": trigger, "scope": scope or ""}
        marker = (trigger, job["scope"])
        # «новая книга»: за одну очередь достаточно одного задания, ключ книги не важен
        if trigger == "workbook":
            marker = (trigger, "*")
        with self._lock:
            if marker in self._pending:
                return False
            self._pending[marker] = job
            self._ensure_thread()
        self._queue.put((marker, job))
        return True

    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._work, name="acl-rules-engine", daemon=True)
            self._thread.start()

    def _work(self) -> None:
        # Блокируемся на очереди без таймаута: поток спит до задания.
        for entry in iter(self._queue.get, _STOP):
            marker, job = entry
            with self._lock:
                self._pending.pop(marker, None)
                self._busy = True
            try:
                self.run_job(job)
            except Exception:  # noqa: BLE001 - оценка не должна ронять поток
                log.exception("Rules evaluation failed (%s)", job)
            finally:
                with self._idle:
                    self._busy = False
                    self._idle.notify_all()

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Дождаться, пока очередь пуста и поток свободен (для тестов и API статуса)."""
        with self._idle:
            return self._idle.wait_for(lambda: not self._pending and not self._busy and self._queue.empty(),
                                       timeout)

    def stop(self) -> None:
        thread = self._thread
        if thread is not None and thread.is_alive():
            self._queue.put(_STOP)
            thread.join(5)

    def status(self) -> dict:
        with self._lock:
            return {"queued": len(self._pending), "busy": self._busy,
                    "worker": bool(self._thread and self._thread.is_alive())}

    # -- оценка --

    def _select_rules(self, job: dict) -> list[dict]:
        rules, self.problems = self.store.list_rules_checked()
        rules = [r for r in rules if r.get("mode") != "disabled"]
        trigger, scope = job["trigger"], job["scope"]
        if trigger == "manual" and scope:
            return [r for r in rules if r["id"] == scope]
        if trigger == "snapshot" and scope:
            def touches(rule: dict) -> bool:
                version = self.store.current_version(rule)
                accounts = (version.get("scope") or {}).get("accounts") or []
                return scope in accounts
            return [r for r in rules if touches(r)]
        return rules

    def run_job(self, job: dict) -> dict:
        """Оценить подходящие правила синхронно. Возвращает сводку (для тестов и API)."""
        now = self._now()
        rules = self._select_rules(job)
        actions = self.store.actions()
        holds = {oid: rec for oid, rec in self.store.active_holds().items()}
        items: list[dict] = []
        failed: list[dict] = []
        for rule in rules:
            version = self.store.current_version(rule)
            try:
                evidence = gather_mod.gather(rule, version, self.sources, now=now, actions=actions, holds=holds)
            except gather_mod.GatherError as exc:
                failed.append({"rule": rule, "version": version, "error": str(exc)})
                continue
            except Exception as exc:  # noqa: BLE001
                log.exception("Could not gather evidence for %s", rule.get("id"))
                failed.append({"rule": rule, "version": version, "error": f"{type(exc).__name__}: {exc}"})
                continue
            result = ev_mod.evaluate(version, evidence, now, mode=rule.get("mode", "observe"))
            items.append({"rule": rule, "version": version, "evidence": evidence, "result": result})

        arbitration = arbiter.arbitrate([{
            "rule_id": i["rule"]["id"], "version": i["version"]["version"], "strategy": i["version"]["strategy"],
            "priority": i["version"].get("priority", 50), "mode": i["rule"].get("mode", "observe"),
            "result": i["result"]} for i in items])

        known = {a.get("dedup_key") for a in actions if a.get("dedup_key")}
        pending = [r for r in self.store.latest_actions().values() if r.get("state") == "proposed"]
        written: list[dict] = []
        records: list[dict] = []
        for i in items:
            rec = self._journal(job, i, arbitration)
            records.append(rec)
            written += self._suggest(i, arbitration, rec, known, pending)
        for f in failed:
            records.append(self.store.append_evaluation({
                "trigger": job["trigger"], "rule_id": f["rule"]["id"], "version": f["version"]["version"],
                "verdict": "blocked", "error": f["error"], "objects": []}))
        return {"evaluations": records, "suggestions": written, "arbitration": arbitration,
                "rules": [i["rule"]["id"] for i in items], "skipped_malformed": list(self.problems)}

    def _journal(self, job: dict, item: dict, arbitration: dict) -> dict:
        rule, version, evidence, result = item["rule"], item["version"], item["evidence"], item["result"]
        objects = []
        for o in result["objects"]:
            rec = {k: o.get(k) for k in ("key", "account", "level", "id", "name", "identity", "verdict",
                                         "reasons", "notes", "observed", "conditions_matched", "action",
                                         "freshness", "automatic_eligible", "fingerprint")}
            decision = arbiter.decision_for(arbitration, rule["id"], o["key"])
            if decision:
                rec["arbiter"] = decision
            objects.append(rec)
        bi = evidence.get("bi") or {}
        return self.store.append_evaluation({
            "trigger": job["trigger"], "rule_id": rule["id"], "version": version["version"],
            "mode": rule.get("mode"), "strategy": version["strategy"], "verdict": result["verdict"],
            "reasons": result["reasons"], "counts": result["counts"],
            "bi_through": bi.get("through"), "bi_built_at": bi.get("built_at"),
            "snapshot_at": evidence.get("snapshot_at"),
            "T": {"value": (evidence.get("target") or {}).get("value"),
                  "source": (evidence.get("target") or {}).get("source")},
            "objects": objects})

    def _suggest(self, item: dict, arbitration: dict, evaluation: dict, known: set,
                 pending: list[dict]) -> list[dict]:
        """Предложения для правил Suggest/Automatic; Observe не предлагает ничего."""
        rule, version = item["rule"], item["version"]
        if rule.get("mode") not in ("suggest", "automatic"):
            return []
        out: list[dict] = []
        for p in arbitration["accepted"]:
            if p["rule_id"] != rule["id"] or p["version"] != version["version"]:
                continue
            key = dedup_key(rule["id"], version["version"], p["key"], p["kind"], p["fingerprint"])
            if key in known:
                continue
            known.add(key)
            action = p["action"]
            obj = next((o for o in item["result"]["objects"] if o["key"] == p["key"]), {})
            # Новые доказательства по тому же правилу, объекту и виду вытесняют старые
            # ожидающие предложения: строки остаются в журнале, но уже `superseded`.
            for old in list(pending):
                if (old.get("rule_id") == rule["id"] and old.get("id") == action.get("id")
                        and old.get("kind") == p["kind"] and old.get("dedup_key") != key):
                    self.store.append_action({**old, "state": "superseded", "superseded_by": key})
                    pending.remove(old)
            out.append(self.store.append_action({
                "state": "proposed", "evaluation_id": evaluation.get("evaluation_id"),
                "rule_id": rule["id"], "version": version["version"], "account": action.get("account"),
                "level": action.get("level"), "id": action.get("id"), "kind": p["kind"],
                "before": action.get("before"), "requested": action.get("requested"),
                "reason": action.get("reason") or "", "dedup_key": key,
                "automatic_eligible": bool(obj.get("automatic_eligible")),
                "freshness": list(obj.get("freshness") or []), "fingerprint": p["fingerprint"],
                "actor": "rules-engine"}))
        return out
