"""Synthesize all analysis into a structured creative brief + designer/AI prompt.

Inputs:
  - scan:     creative_scanner.scan_file() result
  - vision:   vision_analyzer.analyze() result
  - stats:    stats_correlator.correlate() result
  - audience: audience_builder.build_audience_profile() result

Output:
  - Markdown brief saved to creatives/output/<stem>.md
  - Plain-text prompt string for copy-paste into Midjourney / DALL-E / designer brief
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

_PROJ_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

from src.creative_agent.audience_builder import professional_ta_description

_OUTPUT_DIR = _PROJ_ROOT / "creatives" / "output"


def _fmt_usd(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"${v:,.2f}"


def _top_combos_summary(combos: list, label: str, n: int = 3) -> str:
    if not combos:
        return f"  {label}: no data\n"
    lines = [f"  {label} (top {min(n, len(combos))}):\n"]
    for c in combos[:n]:
        interest = c.get("interest") or "—"
        seg = c.get("text_segment") or "—"
        creative = (c.get("creative") or "—")[:40]
        deps = c.get("deposits", 0)
        cpa = _fmt_usd(c.get("cpa"))
        lines.append(f"    • {interest} / {seg} / {creative} → {deps} dep, CPA {cpa}\n")
    return "".join(lines)


def generate_brief(
    scan: Dict[str, Any],
    vision: Dict[str, Any],
    stats: Dict[str, Any],
    audience: Dict[str, Any],
) -> Dict[str, Any]:
    """Build the full creative brief and return dict with 'markdown' and 'prompt' keys."""

    filename = scan.get("filename", "unknown")
    interest = scan.get("interest", "")
    text_segment = scan.get("text_segment", "")
    offer = scan.get("offer_raw", "")
    country = scan.get("country_code", "")
    device = scan.get("device", "all")
    age = scan.get("age", "")
    media_type = scan.get("media_type", "image")

    ta_description = professional_ta_description(audience, country)
    fb_interest = audience.get("fb_interest", "")
    kb_found = audience.get("kb_found", False)

    # Vision insights
    v_scene = vision.get("scene", "")
    v_emotion = vision.get("emotion", "")
    v_hook = vision.get("hook", "")
    v_pattern = vision.get("visual_pattern", "")
    v_style = vision.get("style", "")
    v_colors = vision.get("colors", "")
    v_has_people = vision.get("has_people", False)
    v_strengths = vision.get("strengths", "")
    v_weaknesses = vision.get("weaknesses", "")
    v_cta = vision.get("cta", "")
    v_error = vision.get("error", "")

    # Stats insights
    cell = stats.get("cell", {})
    cell_deps = cell.get("deposits", 0)
    cell_cpa = cell.get("cpa")
    best_creative = stats.get("best_creative", "")

    # Best text segment by deposits across this interest
    by_interest = stats.get("by_interest", [])
    seg_performance: Dict[str, int] = {}
    for c in by_interest:
        seg = c.get("text_segment") or "OTHER"
        seg_performance[seg] = seg_performance.get(seg, 0) + c.get("deposits", 0)
    best_segment = max(seg_performance, key=lambda k: seg_performance[k]) if seg_performance else text_segment

    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ── Build the Markdown brief ───────────────────────────────────────────────

    md_lines = [
        f"# Creative Brief — {filename}",
        f"_Generated: {now}_",
        "",
        "## Source Creative",
        f"- **File:** `{filename}`",
        f"- **Type:** {media_type}",
        f"- **Offer:** {offer or '—'}",
        f"- **Country:** {country or '—'}",
        f"- **Device:** {device}",
        f"- **Age:** {age or '—'}",
        f"- **Interest:** {interest or '—'}",
        f"- **Text Segment:** {text_segment or '—'}",
        "",
        "## Target Audience",
        f"{ta_description}",
    ]

    if fb_interest:
        md_lines += ["", f"**FB Targeting interests:** {fb_interest}"]
    if not kb_found:
        md_lines += ["", "_⚠ Interest not found in KB — check targeting docs._"]

    md_lines += [
        "",
        "## Visual Analysis",
    ]

    if v_error:
        md_lines.append(f"_Vision analysis unavailable: {v_error}_")
    else:
        md_lines += [
            f"- **Scene:** {v_scene}",
            f"- **Emotion:** {v_emotion}",
            f"- **Hook:** {v_hook}",
            f"- **Pattern:** {v_pattern}",
            f"- **Style:** {v_style}  |  **Colors:** {v_colors}",
            f"- **People:** {'Yes' if v_has_people else 'No'}",
            f"- **CTA:** {v_cta or 'none visible'}",
            "",
            f"**What works:** {v_strengths}",
            "",
            f"**What to improve:** {v_weaknesses}",
        ]

    md_lines += [
        "",
        "## Performance Data",
    ]

    if stats.get("excel_found"):
        if cell_deps > 0:
            md_lines.append(
                f"- **This combo** ({interest} × {text_segment}): "
                f"{cell_deps} deposits, CPA {_fmt_usd(cell_cpa)}"
            )
        else:
            md_lines.append(f"- **This combo** ({interest} × {text_segment}): no conversions yet")

        if best_creative:
            md_lines.append(f"- **Best creative for {interest}:** `{best_creative}`")

        md_lines.append(f"- **Best text type for {interest}:** {best_segment}")
        md_lines.append("")
        md_lines.append(_top_combos_summary(by_interest, f"Top by interest ({interest})"))
        md_lines.append(_top_combos_summary(stats.get("by_text_segment", []), f"Top by text type ({text_segment})"))
    else:
        md_lines.append("_Excel not found — no performance data._")

    # ── Designer/AI Prompt ────────────────────────────────────────────────────

    prompt_parts = [
        f"Create a Facebook ad {media_type} creative.",
        "",
        f"Offer: {offer or 'financial product'}",
        f"Country: {country or 'LATAM'}",
        f"Device: {device}",
        "",
        "Target audience:",
        ta_description,
        "",
    ]

    if not v_error:
        prompt_parts += [
            "Based on analysis of the top-performing existing creative:",
            f"- Visual pattern that works: {v_pattern}" if v_pattern else "",
            f"- Keep: {v_strengths}" if v_strengths else "",
            f"- Improve: {v_weaknesses}" if v_weaknesses else "",
            f"- Style: {v_style}" if v_style else "",
            f"- Color palette: {v_colors}" if v_colors else "",
            f"- {'Include real people showing emotion of: ' + v_emotion if v_has_people and v_emotion else 'No people required'}",
            "",
        ]
        prompt_parts = [p for p in prompt_parts if p != ""]

    prompt_parts += [
        f"Ad text type: {best_segment} (this text approach drives the most conversions for this audience).",
        "",
        "Requirements:",
        "- Mobile-first format (9:16 or 4:5)",
        "- Strong visual hook in the first 0.5 seconds / first glance",
        "- Clear value proposition visible without sound",
        f"- CTA: {'Register now' if best_segment.lower() in ('reg', 'registration') else 'Learn more / Start earning'}",
        "- No misleading claims",
    ]

    if best_creative:
        prompt_parts += [
            "",
            f"Reference top creative name for style consistency: {best_creative}",
        ]

    prompt_text = "\n".join(p for p in prompt_parts)

    md_lines += [
        "",
        "## New Creative Prompt",
        "```",
        prompt_text,
        "```",
    ]

    markdown = "\n".join(md_lines)

    # Save output file
    stem = scan.get("stem", "creative")
    out_path = _OUTPUT_DIR / f"{stem}_brief.md"
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8")

    return {
        "markdown": markdown,
        "prompt": prompt_text,
        "output_path": out_path,
    }
