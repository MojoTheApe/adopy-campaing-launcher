"""Markdown rendering for context packs."""
from __future__ import annotations

from src.knowledge_agent.context_pack.models import ContextPack


def render_context_pack(pack: ContextPack) -> str:
    lines = [f"# {pack.title}", ""]
    for name, value in pack.sections.items():
        lines.append(f"## {name}")
        lines.append("")
        lines.append(value.strip() if value.strip() else "_No data._")
        lines.append("")
    lines.append("## Missing fields")
    lines.append("")
    if pack.missing_fields:
        lines.extend(f"- `{field}`" for field in pack.missing_fields)
    else:
        lines.append("_None._")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"
