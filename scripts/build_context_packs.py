#!/usr/bin/env python3
"""Build Markdown context packs from the local knowledge base."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.knowledge_agent.context_pack.builder import ContextPackBuilder
from src.knowledge_agent.context_pack.importers import import_knowledge_json
from src.knowledge_agent.context_pack.markdown import render_context_pack
from src.knowledge_agent.context_pack.store import ContextPackStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build knowledge base context packs.")
    parser.add_argument("--kb-json", default=str(_ROOT / "config" / "knowledge_base.json"))
    parser.add_argument("--db", default=os.environ.get("KB_CONTEXT_DB", str(_ROOT / "data" / "knowledge_context.db")))
    parser.add_argument(
        "--output-dir",
        default=os.environ.get("KB_CONTEXT_OUTPUT_DIR", str(_ROOT / "output" / "context_packs")),
    )
    parser.add_argument("--offer", required=True)
    parser.add_argument("--country", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    kb_path = Path(args.kb_json)
    if not kb_path.is_file():
        print(f"Knowledge JSON not found: {kb_path}", file=sys.stderr)
        return 2

    data = json.loads(kb_path.read_text(encoding="utf-8"))
    store = ContextPackStore(args.db)
    store.initialize()
    import_knowledge_json(store, data)

    builder = ContextPackBuilder(store)
    pack = builder.build_offer_country_pack(offer=args.offer, country=args.country)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"offer_country_{_safe_name(args.offer)}_{_safe_name(args.country)}.md"
    output_path.write_text(render_context_pack(pack), encoding="utf-8")
    print(f"Wrote {output_path}")
    return 0


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in value.strip())


if __name__ == "__main__":
    raise SystemExit(main())
