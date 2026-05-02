"""Build agent-ready context packs from the knowledge store."""
from __future__ import annotations

from collections import Counter

from src.knowledge_agent.context_pack.models import ComboKey, ContextPack
from src.knowledge_agent.context_pack.store import ContextPackStore


class ContextPackBuilder:
    def __init__(self, store: ContextPackStore) -> None:
        self.store = store

    def build_combo_pack(self, key: ComboKey) -> ContextPack:
        offer = self.store.get_offer(key.offer) or {}
        target = self.store.get_target(key.target) or {}
        creative_ctx = self.store.get_creative_context(key.creative)
        creative = creative_ctx.get("creative", {})
        analysis = creative_ctx.get("analysis", {})
        ad_text = self.store.get_ad_text(key.ad_text_key) if key.ad_text_key else None
        ad_text = ad_text or {}
        performance = self.store.get_performance(
            country=key.country,
            offer=key.offer,
            target=key.target,
            creative=key.creative,
        )
        title = f"{key.country} / {key.offer} / {key.target} / {key.creative} / {key.ad_text_key}".rstrip(" /")
        return ContextPack(
            kind="combo",
            title=title,
            sections={
                "Offer meaning": _join_parts(offer.get("usp"), offer.get("audience"), offer.get("notes")),
                "Target audience": _join_parts(target.get("label"), target.get("audience"), target.get("notes")),
                "Creative understanding": _join_parts(analysis.get("visual_summary"), analysis.get("hook")),
                "Ad text": _join_parts(ad_text.get("body"), ad_text.get("hook"), ad_text.get("cta")),
                "Performance memory": _summarize_performance(performance),
                "Why it may work": _why_it_may_work(offer, target, analysis),
                "Risks": _join_parts(
                    ", ".join(ad_text.get("risk_flags", []) or []),
                    ", ".join(analysis.get("risk_flags", []) or []),
                ),
                "Suggested next angles": _suggest_next_angles(offer, target, analysis),
            },
            missing_fields=_missing_combo_fields(offer, target, creative, analysis, ad_text),
            source_ids={"creative": creative.get("name"), "offer": offer.get("name"), "target": target.get("code")},
        )

    def build_creative_pack(self, creative_name: str) -> ContextPack:
        creative_ctx = self.store.get_creative_context(creative_name)
        creative = creative_ctx.get("creative", {})
        analysis = creative_ctx.get("analysis", {})
        performance = self.store.get_performance(creative=creative.get("name", creative_name))
        missing = []
        if not analysis.get("visual_summary"):
            missing.append("creative.visual_summary")
        if not analysis.get("on_screen_text"):
            missing.append("creative.on_screen_text")
        return ContextPack(
            kind="creative",
            title=f"Creative: {creative.get('name', creative_name)}",
            sections={
                "Known aliases": ", ".join(creative.get("aliases", []) or []),
                "Auto vision summary": _join_parts(analysis.get("visual_summary"), analysis.get("hook")),
                "Worked in": _summarize_performance(performance),
                "Similar creatives": "",
            },
            missing_fields=missing,
            source_ids={"creative": creative.get("name", creative_name)},
        )

    def build_offer_country_pack(self, *, offer: str, country: str) -> ContextPack:
        offer_row = self.store.get_offer(offer) or {}
        performance = self.store.get_performance(country=country, offer=offer)
        target_counts: Counter[str] = Counter()
        creative_counts: Counter[str] = Counter()
        for row in performance:
            deposits = int(row.get("deposits", 0) or 0)
            if deposits > 0:
                target_counts[str(row.get("target", ""))] += deposits
                creative_counts[str(row.get("creative", ""))] += deposits
        return ContextPack(
            kind="offer_country",
            title=f"Offer + Country: {offer} / {country}",
            sections={
                "Positioning": _join_parts(offer_row.get("usp"), offer_row.get("audience"), offer_row.get("notes")),
                "Best targets": _format_counter(target_counts),
                "Best creative patterns": _format_counter(creative_counts),
                "Best ad text angles": "",
                "Weak/failed patterns": "",
                "Compliance/risk notes": "",
                "Open gaps": "",
                "New material directions": "",
            },
            missing_fields=[],
            source_ids={"offer": offer_row.get("name", offer), "country": country},
        )


def _missing_combo_fields(offer: dict, target: dict, creative: dict, analysis: dict, ad_text: dict) -> list[str]:
    missing = []
    if not offer:
        missing.append("offer")
    if not target:
        missing.append("target")
    if not creative:
        missing.append("creative")
    if not analysis.get("visual_summary"):
        missing.append("creative.visual_summary")
    if not ad_text.get("body"):
        missing.append("ad_text.body")
    return missing


def _summarize_performance(rows: list[dict]) -> str:
    if not rows:
        return ""
    deposits = sum(int(row.get("deposits", 0) or 0) for row in rows)
    spend = sum(float(row.get("spend", 0) or 0) for row in rows)
    leads = sum(int(row.get("leads", 0) or 0) for row in rows)
    best_roi = max(float(row.get("roi", 0) or 0) for row in rows)
    return f"{deposits} deposits, {leads} leads, ${spend:,.2f} spend, best ROI {best_roi:.2f}"


def _why_it_may_work(offer: dict, target: dict, analysis: dict) -> str:
    parts = []
    if offer.get("usp"):
        parts.append(f"Offer angle: {offer['usp']}")
    if target.get("audience"):
        parts.append(f"Audience fit: {target['audience']}")
    if analysis.get("hook"):
        parts.append(f"Creative hook: {analysis['hook']}")
    return " | ".join(parts)


def _suggest_next_angles(offer: dict, target: dict, analysis: dict) -> str:
    parts = []
    if offer.get("usp"):
        parts.append("Repeat the offer promise with a fresh proof point.")
    if target.get("audience"):
        parts.append("Adapt copy to the target audience's existing intent.")
    if analysis.get("hook"):
        parts.append("Create a variant around the same visual hook.")
    return " ".join(parts)


def _format_counter(counter: Counter[str]) -> str:
    if not counter:
        return ""
    return "\n".join(f"- {name}: {count} deposits" for name, count in counter.most_common(5) if name)


def _join_parts(*values: object) -> str:
    return " | ".join(str(value).strip() for value in values if str(value or "").strip())
