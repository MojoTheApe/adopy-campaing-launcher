from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.fb_ads_rpa.variations.catalog import step_risk
from src.fb_ads_rpa.variations.paths import ranks_json_path

_PUBLISH_RE = re.compile("publish", re.IGNORECASE)
_MAX_OUTCOME_LOG = 500


@dataclass
class StrategyRanks:
    path: Path
    data: dict[str, Any]

    @classmethod
    def load(cls, path: Path | None = None) -> "StrategyRanks":
        ranks_path = path or ranks_json_path()
        if ranks_path.exists():
            data = json.loads(ranks_path.read_text(encoding="utf-8"))
        else:
            data = {}
        return cls(ranks_path, _normalise_data(data))

    def ordered_strategies(self, step_id: str, fallback: list[str]) -> list[str]:
        strategies = self.data["steps"].get(step_id, {}).get("strategies", [])
        ranked = sorted(strategies, key=lambda item: item.get("score", 0), reverse=True)
        ordered = [item["id"] for item in ranked if not _is_publish_strategy(item["id"])]
        known = set(ordered)
        ordered.extend(
            strategy_id
            for strategy_id in fallback
            if strategy_id not in known and not _is_publish_strategy(strategy_id)
        )
        return ordered

    def log_outcome(
        self,
        step_id: str,
        strategy_id: str,
        *,
        ok: bool,
        screenshot: str | None = None,
        browser_crash: bool = False,
    ) -> None:
        _reject_publish(strategy_id)
        self._append_outcome(
            step_id,
            strategy_id,
            ok=ok,
            screenshot=screenshot,
            browser_crash=browser_crash,
        )
        if browser_crash:
            return

        if ok:
            if step_risk(step_id) == "risky":
                self._append_pending_approval(step_id, strategy_id, screenshot)
            else:
                self.data["pending_safe"][step_id] = {
                    "strategy_id": strategy_id,
                    "screenshot": screenshot,
                }
            return

        self._record_failure(step_id, strategy_id)

    def mark_next_step_ok(self, step_id: str) -> None:
        pending = self.data["pending_safe"].pop(step_id, None)
        if not pending:
            return
        self._record_win(step_id, pending["strategy_id"])

    def approve_pending(self, step_id: str, strategy_id: str) -> bool:
        _reject_publish(strategy_id)
        pending = self.data["pending_approval"]
        match = next(
            (
                item
                for item in pending
                if item["step_id"] == step_id and item["strategy_id"] == strategy_id
            ),
            None,
        )
        if match is None:
            return False
        pending.remove(match)
        self._record_win(step_id, strategy_id, promote_to_top=True)
        return True

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _append_outcome(
        self,
        step_id: str,
        strategy_id: str,
        *,
        ok: bool,
        screenshot: str | None,
        browser_crash: bool = False,
    ) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "step_id": step_id,
            "strategy_id": strategy_id,
            "ok": ok,
        }
        if screenshot is not None:
            entry["screenshot"] = screenshot
        if browser_crash:
            entry["browser_crash"] = True
        self.data["outcome_log"].append(entry)
        self.data["outcome_log"] = self.data["outcome_log"][-_MAX_OUTCOME_LOG:]

    def _append_pending_approval(
        self,
        step_id: str,
        strategy_id: str,
        screenshot: str | None,
    ) -> None:
        if any(
            item["step_id"] == step_id and item["strategy_id"] == strategy_id
            for item in self.data["pending_approval"]
        ):
            return
        pending = {
            "step_id": step_id,
            "strategy_id": strategy_id,
        }
        if screenshot is not None:
            pending["screenshot"] = screenshot
        self.data["pending_approval"].append(pending)

    def _record_win(
        self,
        step_id: str,
        strategy_id: str,
        *,
        promote_to_top: bool = False,
    ) -> None:
        strategy = self._strategy_entry(step_id, strategy_id)
        strategy["wins"] += 1
        strategy["score"] += 1
        if promote_to_top:
            self._promote_to_top(step_id, strategy_id)

    def _record_failure(self, step_id: str, strategy_id: str) -> None:
        strategy = self._strategy_entry(step_id, strategy_id)
        strategy["fails"] += 1
        strategy["score"] -= 1

    def _strategy_entry(self, step_id: str, strategy_id: str) -> dict[str, Any]:
        step = self.data["steps"].setdefault(step_id, {"strategies": []})
        strategies = step.setdefault("strategies", [])
        for strategy in strategies:
            if strategy["id"] == strategy_id:
                return strategy
        strategy = {"id": strategy_id, "wins": 0, "fails": 0, "score": 0}
        strategies.append(strategy)
        return strategy

    def _promote_to_top(self, step_id: str, strategy_id: str) -> None:
        strategies = self.data["steps"][step_id]["strategies"]
        top_score = max(
            (strategy["score"] for strategy in strategies if strategy["id"] != strategy_id),
            default=0,
        )
        strategy = self._strategy_entry(step_id, strategy_id)
        strategy["score"] = max(strategy["score"], top_score + 1)


def _normalise_data(data: dict[str, Any]) -> dict[str, Any]:
    data.setdefault("version", 1)
    data.setdefault("steps", {})
    data.setdefault("pending_safe", {})
    data.setdefault("pending_approval", [])
    data.setdefault("outcome_log", [])
    return data


def _reject_publish(strategy_id: str) -> None:
    if _is_publish_strategy(strategy_id):
        raise ValueError(f"Refusing publish strategy: {strategy_id!r}")


def _is_publish_strategy(strategy_id: str) -> bool:
    return bool(_PUBLISH_RE.search(strategy_id))
