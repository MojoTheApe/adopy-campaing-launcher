"""Structured weekly strategy input for the content calendar agent.

Load from JSON/YAML file, from ``CM_WEEK_PLAN_JSON`` (raw JSON string), or
``CM_WEEK_PLAN_PATH`` (path to file). See ``config/content_plan_week.example.json``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class WeeklyPlanInput:
    """Operator-defined constraints for a single planning run."""

    weekly_goals: list[str] = field(default_factory=list)
    max_offer_posts_per_day: int | None = None
    max_offer_posts_per_week: int | None = None
    forbidden_topics: list[str] = field(default_factory=list)
    required_coverage: list[str] = field(default_factory=list)
    exclude_dates: list[str] = field(default_factory=list)
    notes: str = ""

    @classmethod
    def empty(cls) -> WeeklyPlanInput:
        return cls()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WeeklyPlanInput:
        if not data:
            return cls.empty()
        goals = data.get("weekly_goals") or data.get("goals") or []
        if isinstance(goals, str):
            goals = [goals]
        forbidden = data.get("forbidden_topics") or data.get("forbidden") or []
        if isinstance(forbidden, str):
            forbidden = [forbidden]
        required = data.get("required_coverage") or data.get("must_cover") or []
        if isinstance(required, str):
            required = [required]
        exclude = data.get("exclude_dates") or data.get("skip_dates") or []
        if isinstance(exclude, str):
            exclude = [exclude]
        max_day = data.get("max_offer_posts_per_day")
        if max_day is not None:
            max_day = int(max_day)
        max_week = data.get("max_offer_posts_per_week")
        if max_week is not None:
            max_week = int(max_week)
        return cls(
            weekly_goals=[str(x).strip() for x in goals if str(x).strip()],
            max_offer_posts_per_day=max_day,
            max_offer_posts_per_week=max_week,
            forbidden_topics=[str(x).strip() for x in forbidden if str(x).strip()],
            required_coverage=[str(x).strip() for x in required if str(x).strip()],
            exclude_dates=[str(x).strip() for x in exclude if str(x).strip()],
            notes=str(data.get("notes") or "").strip(),
        )

    def to_prompt_block(self, *, lang_es: bool, lang_ru: bool) -> str:
        """Human-readable bullet block for calendar LLM prompts."""
        if lang_es:
            lines: list[str] = ["## Briefing estratégico de la semana (operador)"]
            if self.weekly_goals:
                lines.append("Objetivos:")
                lines.extend(f"- {g}" for g in self.weekly_goals)
            if self.required_coverage:
                lines.append("Cobertura obligatoria / temas a incluir:")
                lines.extend(f"- {r}" for r in self.required_coverage)
            if self.forbidden_topics:
                lines.append("Temas prohibidos o a evitar:")
                lines.extend(f"- {x}" for x in self.forbidden_topics)
            if self.max_offer_posts_per_day is not None:
                lines.append(f"Máx. posts comerciales/agresivos por día: {self.max_offer_posts_per_day}")
            if self.max_offer_posts_per_week is not None:
                lines.append(f"Máx. posts comerciales/agresivos en la semana: {self.max_offer_posts_per_week}")
            if self.exclude_dates:
                lines.append("Fechas sin planificación:")
                lines.extend(f"- {d}" for d in self.exclude_dates)
            if self.notes:
                lines.append(f"Notas: {self.notes}")
            if lines == ["## Briefing estratégico de la semana (operador)"]:
                lines.append("(ninguno — siguen solo pilares de marca)")
            return "\n".join(lines)

        if lang_ru:
            lines = ["## Недельное стратегическое описание (от оператора)"]
            if self.weekly_goals:
                lines.append("Цели недели:")
                lines.extend(f"- {g}" for g in self.weekly_goals)
            if self.required_coverage:
                lines.append("Обязательно отразить / темы:")
                lines.extend(f"- {r}" for r in self.required_coverage)
            if self.forbidden_topics:
                lines.append("Запрещённые или нежелательные темы:")
                lines.extend(f"- {x}" for x in self.forbidden_topics)
            if self.max_offer_posts_per_day is not None:
                lines.append(f"Макс. коммерческих/агрессивных постов в день: {self.max_offer_posts_per_day}")
            if self.max_offer_posts_per_week is not None:
                lines.append(f"Макс. коммерческих/агрессивных постов за неделю: {self.max_offer_posts_per_week}")
            if self.exclude_dates:
                lines.append("Даты без контент-плана:")
                lines.extend(f"- {d}" for d in self.exclude_dates)
            if self.notes:
                lines.append(f"Заметки: {self.notes}")
            if lines == ["## Недельное стратегическое описание (от оператора)"]:
                lines.append("(нет доп. ограничений — только столпы бренда)")
            return "\n".join(lines)

        lines = ["## Operator weekly strategic briefing"]
        if self.weekly_goals:
            lines.append("Goals:")
            lines.extend(f"- {g}" for g in self.weekly_goals)
        if self.required_coverage:
            lines.append("Required coverage:")
            lines.extend(f"- {r}" for r in self.required_coverage)
        if self.forbidden_topics:
            lines.append("Forbidden or avoid topics:")
            lines.extend(f"- {x}" for x in self.forbidden_topics)
        if self.max_offer_posts_per_day is not None:
            lines.append(f"Max aggressive/commercial posts per day: {self.max_offer_posts_per_day}")
        if self.max_offer_posts_per_week is not None:
            lines.append(f"Max aggressive/commercial posts this week: {self.max_offer_posts_per_week}")
        if self.exclude_dates:
            lines.append("Skip planning dates:")
            lines.extend(f"- {d}" for d in self.exclude_dates)
        if self.notes:
            lines.append(f"Notes: {self.notes}")
        if lines == ["## Operator weekly strategic briefing"]:
            lines.append("(none — rely on brand pillars only)")
        return "\n".join(lines)


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "Install PyYAML to load .yaml/.yml week plans (pip install pyyaml).",
        ) from e
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"Week plan YAML root must be a mapping, got {type(data)!r}")
    return data


def load_weekly_plan_input(path: str | Path) -> WeeklyPlanInput:
    """Load constraints from JSON or YAML file."""
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"Week plan file not found: {p}")
    suffix = p.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        return WeeklyPlanInput.from_dict(_load_yaml(p))
    if suffix == ".json" or suffix == "":
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("Week plan JSON root must be an object")
        return WeeklyPlanInput.from_dict(raw)
    return WeeklyPlanInput.from_dict(json.loads(p.read_text(encoding="utf-8")))


def load_weekly_plan_input_from_env() -> WeeklyPlanInput | None:
    """Load from CM_WEEK_PLAN_JSON or CM_WEEK_PLAN_PATH; None if unset."""
    json_str = os.environ.get("CM_WEEK_PLAN_JSON", "").strip()
    if json_str:
        raw = json.loads(json_str)
        if not isinstance(raw, dict):
            raise ValueError("CM_WEEK_PLAN_JSON must be a JSON object")
        return WeeklyPlanInput.from_dict(raw)
    env_path = os.environ.get("CM_WEEK_PLAN_PATH", "").strip()
    if env_path:
        return load_weekly_plan_input(env_path)
    return None
