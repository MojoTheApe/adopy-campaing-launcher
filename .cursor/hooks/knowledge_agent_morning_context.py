#!/usr/bin/env python3
"""Auto-build Knowledge Agent context for morning-report discussions.

The hook is intentionally fail-open: it writes a latest context file when it
detects a morning-report/brainstorming prompt, then allows the prompt through.
Cursor rules tell the agent to read that file before answering.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.knowledge_agent.context_pack.builder import ContextPackBuilder
from src.knowledge_agent.context_pack.importers import import_knowledge_json
from src.knowledge_agent.context_pack.markdown import render_context_pack
from src.knowledge_agent.context_pack.store import ContextPackStore

KB_JSON = PROJECT_ROOT / "config" / "knowledge_base.json"
DB_PATH = PROJECT_ROOT / os.environ.get("KB_CONTEXT_DB", "data/knowledge_context.db")
OUT_DIR = PROJECT_ROOT / os.environ.get("KB_CONTEXT_OUTPUT_DIR", "output/context_packs")
LATEST_CONTEXT = OUT_DIR / "latest_auto_context.md"

COUNTRY_ALIASES = {
    "ecuador": "EC",
    "ec": "EC",
    "mexico": "MX",
    "méxico": "MX",
    "mx": "MX",
    "chile": "CL",
    "cl": "CL",
    "colombia": "CO",
    "co": "CO",
    "argentina": "AR",
    "ar": "AR",
    "guatemala": "GT",
    "gt": "GT",
    "uae": "UAE",
    "united arab emirates": "UAE",
    "emirates": "UAE",
    "оаэ": "UAE",
    "kuwait": "KW",
    "kw": "KW",
    "bahrain": "BH",
    "bh": "BH",
    "oman": "OM",
    "om": "OM",
    "qatar": "QA",
    "qa": "QA",
    "brazil": "BR",
    "br": "BR",
}

RELEVANCE_MARKERS = (
    "morning report",
    "country:",
    "working combos",
    "what to open",
    "what to close",
    "hypotheses",
    "ai context",
    "связк",
    "креатив",
    "оффер",
    "таргет",
    "брейнсторм",
    "утрен",
)


def main() -> int:
    try:
        event = json.loads(sys.stdin.read() or "{}")
        prompt = _extract_text(event)
        prompt = _append_referenced_file_context(prompt)
        if not _is_relevant(prompt):
            _allow()
            return 0

        context = _build_context(prompt)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        LATEST_CONTEXT.write_text(context, encoding="utf-8")
        _allow()
        return 0
    except Exception as exc:  # noqa: BLE001 - hook must never block chat
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        LATEST_CONTEXT.write_text(
            "# Knowledge Agent Auto Context\n\n"
            f"Hook failed open: {type(exc).__name__}: {exc}\n",
            encoding="utf-8",
        )
        _allow()
        return 0


def _allow() -> None:
    print(json.dumps({"permission": "allow"}, ensure_ascii=False))


def _extract_text(value: Any) -> str:
    parts: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            parts.append(node)
            return
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if isinstance(node, dict):
            priority_keys = (
                "prompt",
                "userPrompt",
                "user_prompt",
                "text",
                "message",
                "content",
                "input",
                "arguments",
            )
            for key in priority_keys:
                if key in node:
                    walk(node[key])
            return

    walk(value)
    return "\n".join(parts)[:100_000]


def _append_referenced_file_context(prompt: str) -> str:
    snippets = []
    for match in re.finditer(r"@([A-Za-z]:\\[^\s:]+(?:\\[^\s:]+)*?\.md):(\d+)-(\d+)", prompt):
        path = Path(match.group(1))
        start = int(match.group(2))
        end = int(match.group(3))
        if not path.is_file() or start < 1 or end < start:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        snippets.append("\n".join(lines[start - 1 : end]))
    if not snippets:
        return prompt
    return prompt + "\n\n" + "\n\n".join(snippets)


def _is_relevant(prompt: str) -> bool:
    lowered = prompt.lower()
    return any(marker in lowered for marker in RELEVANCE_MARKERS)


def _build_context(prompt: str) -> str:
    if not KB_JSON.is_file():
        return "# Knowledge Agent Auto Context\n\n`config/knowledge_base.json` not found.\n"

    data = json.loads(KB_JSON.read_text(encoding="utf-8"))
    store = ContextPackStore(DB_PATH)
    store.initialize()
    import_knowledge_json(store, data)

    offers = _detect_offers(prompt, data.get("offers", {}))
    countries = _detect_countries(prompt)
    if not countries:
        countries = _infer_countries_from_offers(offers, data.get("offers", {}))

    lines = [
        "# Knowledge Agent Auto Context",
        "",
        "Auto-generated because the user prompt looks related to morning report, working/non-working combos, creatives, offers, targets, or brainstorming.",
        "",
    ]

    if not offers:
        lines.extend(
            [
                "## Detection",
                "",
                "- No known offer name was detected in the prompt.",
                "- Use the copied morning report text to identify the offer/target/creative manually.",
                "",
            ]
        )
        return "\n".join(lines).rstrip() + "\n"

    if not countries:
        lines.extend(
            [
                "## Detection",
                "",
                "- Detected offers: " + ", ".join(offers),
                "- No country was detected. Ask the user which geo to analyze if it is not clear from the pasted context.",
                "",
            ]
        )
        return "\n".join(lines).rstrip() + "\n"

    builder = ContextPackBuilder(store)
    pair_count = 0
    for offer in offers:
        for country in countries:
            if pair_count >= 5:
                lines.extend(["", "_Limited to first 5 detected offer/country context packs._", ""])
                return "\n".join(lines).rstrip() + "\n"
            pack = builder.build_offer_country_pack(offer=offer, country=country)
            lines.append(render_context_pack(pack).rstrip())
            lines.append("")
            pair_count += 1

    return "\n".join(lines).rstrip() + "\n"


def _detect_offers(prompt: str, offers: dict[str, Any]) -> list[str]:
    lowered = prompt.lower()
    found: list[str] = []
    for name, payload in offers.items():
        candidates = [str(name)]
        if isinstance(payload, dict):
            candidates.extend(str(alias) for alias in payload.get("aliases", []) or [])
        if any(_contains_phrase(lowered, candidate) for candidate in candidates if len(candidate.strip()) >= 3):
            found.append(str(name))
    return found[:8]


def _detect_countries(prompt: str) -> list[str]:
    lowered = prompt.lower()
    found: list[str] = []

    for match in re.finditer(r"(?:^|\n)\s*(?:#+\s*)?country:\s*([^\n\r|]+)", prompt, flags=re.IGNORECASE):
        raw = match.group(1).strip().lower()
        normalized = COUNTRY_ALIASES.get(raw, raw.upper())
        if normalized and normalized not in found:
            found.append(normalized)

    for alias, code in COUNTRY_ALIASES.items():
        if _contains_phrase(lowered, alias) and code not in found:
            found.append(code)

    return found[:5]


def _infer_countries_from_offers(offers: list[str], all_offers: dict[str, Any]) -> list[str]:
    inferred: list[str] = []
    for offer in offers:
        payload = all_offers.get(offer)
        if not isinstance(payload, dict):
            continue
        for country in payload.get("top_countries", []) or []:
            country_s = str(country).strip().upper()
            if country_s and country_s not in inferred:
                inferred.append(country_s)
    return inferred[:5]


def _contains_phrase(haystack_lower: str, phrase: str) -> bool:
    phrase_lower = phrase.strip().lower()
    if not phrase_lower:
        return False
    if re.fullmatch(r"[a-z]{2,3}", phrase_lower):
        return re.search(rf"(?<![a-z0-9]){re.escape(phrase_lower)}(?![a-z0-9])", haystack_lower) is not None
    return phrase_lower in haystack_lower


if __name__ == "__main__":
    raise SystemExit(main())
