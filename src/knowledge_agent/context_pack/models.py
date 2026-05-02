"""Typed data objects for knowledge base context packs."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ComboKey:
    country: str
    offer: str
    target: str
    creative: str
    ad_text_key: str = ""


@dataclass
class ContextPack:
    kind: str
    title: str
    sections: dict[str, str] = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    source_ids: dict[str, Any] = field(default_factory=dict)

    def section(self, name: str) -> str:
        return self.sections.get(name, "").strip()
