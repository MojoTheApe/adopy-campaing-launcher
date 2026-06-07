"""Build fresh asset-news context for Morning Report country sections.

The module keeps source fetching separate from report aggregation. Tests can
pass a fake fetcher; production uses a small hybrid of Google News RSS and
GDELT, with conservative summaries based on retrieved titles/snippets.

Branded offers (Amazon, Mercado Libre, …) use brand-centric queries.
Trolley / placeholder offers use thematic queries derived from working combos
(Day, Trading, Reading) plus knowledge-base target context.
"""
from __future__ import annotations

import email.utils
import html
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Sequence
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
import xml.etree.ElementTree as ET

import requests

from src.knowledge_agent.knowledge_base import get_offer_context, get_target_context, resolve_target_code

NewsResult = Dict[str, Any]
NewsFetcher = Callable[[str], List[NewsResult]]

_LOCAL_NEWS_TERMS: Dict[str, str] = {
    "mexico": "noticias acciones bolsa regulación",
    "guatemala": "noticias acciones bolsa regulación",
    "chile": "noticias acciones bolsa regulación",
    "colombia": "noticias acciones bolsa regulación",
    "peru": "noticias acciones bolsa regulación",
    "argentina": "noticias acciones bolsa regulación",
    "ecuador": "noticias acciones bolsa regulación",
    "venezuela": "noticias acciones bolsa regulación",
    "bolivia": "noticias acciones bolsa regulación",
    "brazil": "notícias ações bolsa regulação",
    "kuwait": "أخبار أسهم سوق تنظيم",
    "uae": "أخبار أسهم سوق تنظيم",
    "united arab emirates": "أخبار أسهم سوق تنظيم",
    "qatar": "أخبار أسهم سوق تنظيم",
    "oman": "أخبار أسهم سوق تنظيم",
    "bahrain": "أخبار أسهم سوق تنظيم",
    "saudi arabia": "أخبار أسهم سوق تنظيم",
}

_RSS_LOCALE: Dict[str, tuple[str, str, str]] = {
    "mexico": ("MX", "es-419", "MX:es-419"),
    "guatemala": ("GT", "es-419", "GT:es-419"),
    "chile": ("CL", "es-419", "CL:es-419"),
    "colombia": ("CO", "es-419", "CO:es-419"),
    "peru": ("PE", "es-419", "PE:es-419"),
    "argentina": ("AR", "es-419", "AR:es-419"),
    "ecuador": ("EC", "es-419", "EC:es-419"),
    "venezuela": ("VE", "es-419", "VE:es-419"),
    "bolivia": ("BO", "es-419", "BO:es-419"),
    "brazil": ("BR", "pt-BR", "BR:pt-419"),
    "kuwait": ("KW", "en", "KW:en"),
    "uae": ("AE", "en", "AE:en"),
    "united arab emirates": ("AE", "en", "AE:en"),
    "qatar": ("QA", "en", "QA:en"),
    "oman": ("OM", "en", "OM:en"),
    "bahrain": ("BH", "en", "BH:en"),
    "saudi arabia": ("SA", "en", "SA:en"),
}

_REPUTABLE_SOURCES = {
    "reuters",
    "bloomberg",
    "cnbc",
    "marketwatch",
    "financial times",
    "ft",
    "wsj",
    "the wall street journal",
}

_AI_TRADING_DEDUPE_KEY = "__ai_trading_family__"
_MIN_RELEVANCE_SCORE = 4

_DEFAULT_THEMATIC_INTERESTS = ("Trading", "Day")

_THEME_MATCH_TERMS = (
    "trading",
    "trader",
    "forex",
    "stock",
    "stocks",
    "share",
    "shares",
    "invest",
    "investor",
    "market",
    "bolsa",
    "acciones",
    "broker",
    "finance",
    "financial",
    "ipo",
    "earnings",
)

_SPORTS_NOISE_TERMS = (
    "betting",
    "predictions",
    "football",
    "soccer",
    "world cup",
    " vs ",
    " vs. ",
)

_SPORTS_ALLOWED_INTERESTS = {
    "gmbl",
    "sport ar com",
    "gambling ar com",
}

_BROKER_REVIEW_TERMS = (
    "traders union",
    "exness review",
    "broker review",
)

_GLOBAL_BRANDS = {
    "amazon",
    "apple",
    "google",
    "microsoft",
    "meta",
    "tesla",
    "nvidia",
    "mercado libre",
    "meli",
}


@dataclass(frozen=True)
class NewsAssetPlan:
    dedupe_key: str
    display_label: str
    strategy: str  # branded | thematic | ai_trading
    news_subject: str


@dataclass(frozen=True)
class InterestWeight:
    interest: str
    deposits: int
    spend: float


def normalize_asset_key(asset: str) -> str:
    """Lowercase key for deduping offer spellings (underscores, spacing)."""
    s = str(asset or "").strip().lower().replace("_", " ").replace("-", " ")
    return " ".join(s.split())


def _is_ai_trading_family(normalized_key: str) -> bool:
    """True for AI Trader / AI_Trader / Trader AI style product names."""
    if not normalized_key:
        return False
    compact = normalized_key.replace(" ", "")
    if "aitrader" in compact or "traderai" in compact:
        return True
    words = set(normalized_key.split())
    if "ai" in words and "trader" in words:
        return True
    if "ai" in words and "trading" in words:
        return True
    return False


def _is_trolley_family(normalized_key: str) -> bool:
    """True for Trolley placeholder offers and Trolley Form variants."""
    if not normalized_key:
        return False
    if normalized_key == "trolley":
        return True
    return normalized_key.startswith("trolley ")


def classify_news_asset(asset: str) -> NewsAssetPlan:
    """Classify one offer for news search (branded vs thematic vs AI trading)."""
    raw = str(asset or "").strip()
    if not raw:
        return NewsAssetPlan("", "", "branded", "")
    key = normalize_asset_key(raw)
    if _is_ai_trading_family(key):
        return NewsAssetPlan(_AI_TRADING_DEDUPE_KEY, "AI Trader", "ai_trading", "AI trading")
    if _is_trolley_family(key):
        return NewsAssetPlan(key, raw, "thematic", "")
    return NewsAssetPlan(key, raw, "branded", raw)


def canonical_news_asset(asset: str) -> tuple[str, str, str]:
    """Return (dedupe_key, display_label, news_query_subject) — legacy helper."""
    plan = classify_news_asset(asset)
    subject = plan.news_subject or plan.display_label
    return plan.dedupe_key, plan.display_label, subject


def merge_assets_for_news(assets: Iterable[str]) -> List[NewsAssetPlan]:
    """Stable unique list of classified assets for one country."""
    seen: set[str] = set()
    out: List[NewsAssetPlan] = []
    for asset in assets:
        plan = classify_news_asset(str(asset or ""))
        if not plan.dedupe_key:
            continue
        if plan.dedupe_key in seen:
            continue
        seen.add(plan.dedupe_key)
        out.append(plan)
    return out


def _offer_news_key(offer: str) -> str:
    plan = classify_news_asset(offer)
    return plan.dedupe_key or normalize_asset_key(offer)


def extract_top_interests_for_offer(
    top_combos: Sequence[Dict[str, Any]],
    offer: str,
    *,
    max_interests: int = 2,
    fallback_to_defaults: bool = True,
) -> List[InterestWeight]:
    """Pick top interests from working combos for a thematic offer."""
    offer_s = str(offer or "").strip()
    if not offer_s:
        return []
    offer_key = _offer_news_key(offer_s)

    by_interest: Dict[str, InterestWeight] = {}
    for combo in top_combos:
        if _offer_news_key(str(combo.get("offer", "")).strip()) != offer_key:
            continue
        interest = str(combo.get("interest", "")).strip()
        if not interest or interest.lower() in ("", "nan", "wide", "none", "-"):
            continue
        deposits = int(combo.get("deposits") or 0)
        spend = float(combo.get("spend") or 0.0)
        existing = by_interest.get(interest)
        if existing is None:
            by_interest[interest] = InterestWeight(interest, deposits, spend)
        else:
            by_interest[interest] = InterestWeight(
                interest,
                existing.deposits + deposits,
                existing.spend + spend,
            )

    ranked = sorted(
        by_interest.values(),
        key=lambda row: (row.deposits, row.spend),
        reverse=True,
    )
    if ranked:
        return ranked[:max_interests]

    if fallback_to_defaults:
        return [InterestWeight(name, 0, 0.0) for name in _DEFAULT_THEMATIC_INTERESTS[:max_interests]]
    return []


def _theme_phrase_for_interest(interest: str) -> str:
    """Build a short English search phrase from KB target context."""
    raw = str(interest or "").strip()
    if not raw:
        return ""
    canonical = resolve_target_code(raw) or raw
    ctx = get_target_context(canonical)
    if ctx is None:
        return raw
    label = str(ctx.get("label") or canonical).strip()
    fb_interest = str(ctx.get("fb_interest") or "")
    parts = [p.strip() for p in fb_interest.split(",") if p.strip()][:2]
    if parts:
        return f"{label} {' '.join(parts)}"
    return label


def build_thematic_queries(interests: Sequence[str], country: str) -> List[str]:
    """Return thematic query bundle from combo interests + country."""
    country_s = str(country or "").strip()
    queries: List[str] = []
    for interest in interests[:2]:
        phrase = _theme_phrase_for_interest(interest)
        if not phrase:
            continue
        queries.append(f"{phrase} {country_s} news last 7 days")
        queries.append(f"{phrase} retail investors regulation {country_s}")
    local_terms = _LOCAL_NEWS_TERMS.get(country_s.lower())
    if local_terms and interests:
        phrase = _theme_phrase_for_interest(interests[0])
        if phrase:
            queries.append(f"{phrase} {country_s} {local_terms}")
    return queries


def build_branded_queries(
    brand: str,
    country: str,
    *,
    combo_interests: Sequence[str] | None = None,
) -> List[str]:
    """Return brand-centric query bundle for a named offer."""
    brand_s = str(brand or "").strip()
    country_s = str(country or "").strip()
    search_name = brand_s

    queries = [
        f"{search_name} {country_s} news last 7 days",
        f"{search_name} stock shares {country_s} news last 7 days",
        f"{search_name} earnings partnership regulation {country_s}",
    ]
    local_terms = _LOCAL_NEWS_TERMS.get(country_s.lower())
    if local_terms:
        queries.append(f"{search_name} {country_s} {local_terms}")
    if combo_interests:
        queries.extend(build_thematic_queries(combo_interests[:1], country)[:1])
    return queries


def build_asset_queries(
    news_subject: str,
    country: str,
    *,
    ai_trading: bool = False,
    strategy: str = "branded",
    thematic_interests: Sequence[str] | None = None,
    combo_interests: Sequence[str] | None = None,
) -> List[str]:
    """Return the hybrid query bundle for one asset/country pair."""
    if strategy == "thematic":
        return build_thematic_queries(thematic_interests or _DEFAULT_THEMATIC_INTERESTS, country)
    if ai_trading or strategy == "ai_trading":
        country_s = str(country or "").strip()
        queries = [
            f"AI trading artificial intelligence {country_s} news last 7 days",
            f"retail AI trading apps brokers {country_s} regulation news",
            f"AI trading investor sentiment algorithmic trading news",
        ]
        local_terms = _LOCAL_NEWS_TERMS.get(country_s.lower())
        if local_terms:
            queries.append(f"AI trading {country_s} {local_terms}")
        if combo_interests:
            queries.extend(build_thematic_queries(combo_interests[:1], country)[:1])
        return queries
    return build_branded_queries(
        news_subject,
        country,
        combo_interests=combo_interests,
    )


def google_news_rss_url(query: str, country: str | None = None) -> str:
    hl, gl, ceid = "en-US", "US", "US:en"
    locale = _RSS_LOCALE.get(str(country or "").strip().lower())
    if locale is not None:
        gl, hl, ceid = locale
    params = urlencode({"q": query, "hl": hl, "gl": gl, "ceid": ceid})
    return f"https://news.google.com/rss/search?{params}"


def gdelt_doc_url(query: str, days: int = 7) -> str:
    params = urlencode(
        {
            "query": query,
            "mode": "artlist",
            "format": "json",
            "timespan": f"{days}d",
            "maxrecords": "10",
            "sort": "hybridrel",
        }
    )
    return f"https://api.gdeltproject.org/api/v2/doc/doc?{params}"


def _strip_tracking(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return str(url or "").strip()
    query = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith("utm_")
    ]
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/"),
            "",
            urlencode(query),
            "",
        )
    )


def parse_news_datetime(value: Any) -> datetime | None:
    """Normalize RSS/GDELT publication timestamps to timezone-aware UTC."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit() and len(s) == 14:
        try:
            return datetime.strptime(s, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        try:
            return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _age_days_since(pub: datetime, reference: datetime) -> float:
    pub_u = pub.astimezone(timezone.utc)
    ref_u = reference.astimezone(timezone.utc)
    return (ref_u - pub_u).total_seconds() / 86400.0


def dedupe_results(results: Iterable[NewsResult]) -> List[NewsResult]:
    """Dedupe by normalized URL first, then normalized title."""
    seen: set[str] = set()
    out: List[NewsResult] = []
    for result in results:
        url_key = _strip_tracking(str(result.get("url", "")))
        title_key = " ".join(str(result.get("title", "")).lower().split())
        key = url_key or title_key
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(dict(result))
    return out


def _allows_sports_noise(interests: Sequence[str]) -> bool:
    for interest in interests:
        key = normalize_asset_key(interest)
        if key in _SPORTS_ALLOWED_INTERESTS:
            return True
    return False


def _is_global_brand(match_subject: str) -> bool:
    return normalize_asset_key(match_subject) in _GLOBAL_BRANDS


def _score_result(
    result: NewsResult,
    *,
    strategy: str,
    match_subject: str,
    country: str,
    theme_terms: Sequence[str],
    interests: Sequence[str],
) -> int:
    text = f"{result.get('title', '')} {result.get('snippet', '')}".lower()
    source = str(result.get("source", "")).lower()
    score = 0
    subj = str(match_subject or "").strip().lower()
    country_l = country.lower()

    if strategy == "thematic":
        for term in theme_terms:
            term_l = term.lower()
            if term_l and term_l in text:
                score += 3
        for interest in interests:
            phrase = _theme_phrase_for_interest(interest).lower()
            for word in phrase.split():
                if len(word) > 3 and word in text:
                    score += 1
        for term in _THEME_MATCH_TERMS:
            if term in text:
                score += 1
    elif strategy == "ai_trading":
        if subj and subj in text:
            score += 5
        if ("ai" in text or "artificial intelligence" in text) and (
            "trad" in text or "broker" in text or "invest" in text
        ):
            score += 4
    else:
        if subj:
            if subj in text:
                score += 5
            else:
                brand_words = [w for w in subj.split() if len(w) > 2]
                hits = sum(1 for w in brand_words if w in text)
                if hits:
                    score += 2 * hits
                elif brand_words:
                    score -= 10
        if normalize_asset_key(match_subject) == "mercado libre" and "meli" in text:
            score += 3
        for term in theme_terms:
            term_l = term.lower()
            if term_l and term_l in text:
                score += 2
        for interest in interests:
            phrase = _theme_phrase_for_interest(interest).lower()
            for word in phrase.split():
                if len(word) > 3 and word in text:
                    score += 1

    if country_l in text:
        score += 2
    elif strategy == "branded" and not _is_global_brand(match_subject):
        score -= 4

    if not _allows_sports_noise(interests) and any(term in text for term in _SPORTS_NOISE_TERMS):
        score -= 8

    if strategy != "ai_trading" and any(term in text for term in _BROKER_REVIEW_TERMS):
        score -= 3

    if any(src in source for src in _REPUTABLE_SOURCES):
        score += 2
    if "press release" in text or "investor relations" in text:
        score += 1
    return score


def _signal_for(text: str) -> str:
    lower = text.lower()
    if any(w in lower for w in ("earnings", "revenue", "profit", "quarter")):
        return "earnings / financial update"
    if any(w in lower for w in ("regulation", "regulator", "lawsuit", "fine", "ban")):
        return "regulatory / risk story"
    if any(w in lower for w in ("launch", "expands", "partnership", "deal", "investment")):
        return "local business story"
    if any(w in lower for w in ("shares", "stock", "market", "rally", "falls")):
        return "market move"
    return "recent coverage"


def _fallback_item(
    asset: str,
    *,
    query_basis: str | None = None,
    theme_label: str | None = None,
    interest_source: str | None = None,
) -> Dict[str, Any]:
    item = {
        "asset": asset,
        "signal": "no strong fresh news found",
        "summary": "No reliable recent asset news found.",
        "headline_angle": "Use evergreen angles from working combos rather than newsjacking.",
        "confidence": "low",
        "freshness_days": None,
    }
    if theme_label:
        item["theme_label"] = theme_label
    if interest_source:
        item["interest_source"] = interest_source
    if query_basis:
        item["query_basis"] = query_basis
    return item


def _format_query_basis(interest_rows: Sequence[InterestWeight]) -> str:
    parts = []
    for row in interest_rows:
        if row.deposits > 0:
            parts.append(f"{row.interest} ({row.deposits} dep)")
        elif row.spend > 0:
            parts.append(f"{row.interest} (${row.spend:,.0f} spend)")
        else:
            parts.append(row.interest)
    if not parts:
        return ""
    return "top combos — " + ", ".join(parts)


def _rank_fresh_news_results(
    results: List[NewsResult],
    *,
    strategy: str,
    match_subject: str,
    country: str,
    theme_terms: Sequence[str],
    interests: Sequence[str],
    reference_now: datetime,
    max_age_days: float,
) -> List[NewsResult]:
    """Keep deduped hits younger than ``max_age_days``; prefer relevance then recency."""
    deduped = dedupe_results(results)
    scored: List[tuple[NewsResult, float | None, int]] = []
    for r in deduped:
        pub = parse_news_datetime(r.get("published_at"))
        relevance = _score_result(
            r,
            strategy=strategy,
            match_subject=match_subject,
            country=country,
            theme_terms=theme_terms,
            interests=interests,
        )
        if relevance < _MIN_RELEVANCE_SCORE:
            continue
        if pub is None:
            scored.append((r, None, relevance))
            continue
        age = _age_days_since(pub, reference_now)
        if age > max_age_days:
            continue
        scored.append((r, age, relevance))
    if not scored:
        return []

    if country.lower() in _LOCAL_NEWS_TERMS:
        country_scored = [
            row
            for row in scored
            if country.lower()
            in f"{row[0].get('title', '')} {row[0].get('snippet', '')}".lower()
        ]
        if country_scored:
            scored = country_scored

    def sort_key(row: tuple[NewsResult, float | None, int]) -> tuple[int, int, int, float]:
        r, age, relevance = row
        text = f"{r.get('title', '')} {r.get('snippet', '')}".lower()
        country_boost = 1 if country.lower() in text else 0
        if age is None:
            return (1, -relevance, -country_boost, 0.0)
        return (0, -relevance, -country_boost, age)

    scored.sort(key=sort_key)
    return [pair[0] for pair in scored]


def _summarize_asset(
    asset_display: str,
    country: str,
    results: List[NewsResult],
    *,
    strategy: str,
    match_subject: str,
    interests: Sequence[str],
    theme_terms: Sequence[str],
    query_basis: str | None,
    theme_label: str | None,
    reference_now: datetime,
    max_news_age_days: float,
) -> Dict[str, Any]:
    ranked = _rank_fresh_news_results(
        results,
        strategy=strategy,
        match_subject=match_subject,
        country=country,
        theme_terms=theme_terms,
        interests=interests,
        reference_now=reference_now,
        max_age_days=max_news_age_days,
    )
    if not ranked:
        return _fallback_item(
            asset_display,
            query_basis=query_basis,
            theme_label=theme_label,
            interest_source="combo" if strategy == "branded" and theme_label else None,
        )

    top = ranked[0]
    title = str(top.get("title", "")).strip()
    snippet = str(top.get("snippet", "")).strip()
    text = f"{title}. {snippet}".strip()
    sources = []
    for result in ranked[:3]:
        source = str(result.get("source", "")).strip()
        if source and source not in sources:
            sources.append(source)

    signal = _signal_for(text)
    top_pub = parse_news_datetime(top.get("published_at"))
    freshness_days: float | None
    if top_pub is not None:
        freshness_days = round(_age_days_since(top_pub, reference_now), 1)
    else:
        freshness_days = None

    if strategy == "ai_trading":
        angle = (
            f"Tie {asset_display} headlines to AI-assisted trading trends and platform news "
            f"in {country} — stay factual, avoid hype."
        )
    elif strategy == "thematic":
        angle = (
            f"Tie {asset_display} headlines to {signal} themes ({theme_label or 'trading'}) "
            f"in {country} — stay factual, avoid hype."
        )
    elif theme_label:
        angle = (
            f"Connect {asset_display} to {signal} in {country} "
            f"using combo interests ({theme_label}) — stay factual, avoid hype."
        )
    else:
        angle = f"Connect {asset_display} to {signal} in {country} with a cautious, timely hook."

    item: Dict[str, Any] = {
        "asset": asset_display,
        "signal": signal,
        "summary": text[:240],
        "headline_angle": angle,
        "confidence": "medium" if sources else "low",
        "freshness_days": freshness_days,
    }
    if theme_label:
        item["theme_label"] = theme_label
        if strategy == "branded":
            item["interest_source"] = "combo"
    if query_basis:
        item["query_basis"] = query_basis
    return item


def build_news_context(
    country: str,
    assets: Iterable[str],
    *,
    top_combos: Sequence[Dict[str, Any]] | None = None,
    fetcher: NewsFetcher | None = None,
    max_assets: int = 3,
    max_queries_per_asset: int | None = None,
    reference_now: datetime | None = None,
    max_news_age_days: int | None = None,
) -> Dict[str, Any]:
    """Fetch and normalize news context for assets in one country.

    Rows older than ``max_news_age_days`` (env ``NEWS_MAX_AGE_DAYS``, default 7)
    are dropped when ``published_at`` parses; undated rows are kept but ranked
    after dated fresh items.
    """
    fetch = fetcher or (lambda query: fetch_hybrid_news(query, country=country))
    ref = reference_now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    else:
        ref = ref.astimezone(timezone.utc)
    max_age = float(
        max_news_age_days
        if max_news_age_days is not None
        else max(1, int(os.environ.get("NEWS_MAX_AGE_DAYS", "7")))
    )
    query_limit = max_queries_per_asset
    if query_limit is None:
        query_limit = max(1, int(os.environ.get("NEWS_MAX_QUERIES_PER_ASSET", "4")))

    combo_rows = list(top_combos or [])
    items: List[Dict[str, Any]] = []
    merged = merge_assets_for_news(assets)
    for plan in merged:
        interest_rows: List[InterestWeight] = []
        theme_label: str | None = None
        query_basis: str | None = None
        theme_terms: List[str] = []
        interests: List[str] = []
        match_subject = plan.news_subject

        if plan.strategy == "thematic":
            interest_rows = extract_top_interests_for_offer(combo_rows, plan.display_label)
            interests = [row.interest for row in interest_rows]
            theme_label = " + ".join(interests) if interests else None
            query_basis = _format_query_basis(interest_rows) or None
            theme_terms = [_theme_phrase_for_interest(i) for i in interests]
            match_subject = " ".join(theme_terms)
        elif plan.strategy in ("branded", "ai_trading") and combo_rows:
            interest_rows = extract_top_interests_for_offer(
                combo_rows,
                plan.display_label,
                max_interests=2,
                fallback_to_defaults=False,
            )
            if interest_rows:
                interests = [row.interest for row in interest_rows]
                theme_label = " + ".join(interests)
                query_basis = _format_query_basis(interest_rows) or None
                theme_terms = [_theme_phrase_for_interest(i) for i in interests]

        results: List[NewsResult] = []
        qpack = build_asset_queries(
            plan.news_subject or plan.display_label,
            country,
            ai_trading=plan.strategy == "ai_trading",
            strategy=plan.strategy,
            thematic_interests=interests,
            combo_interests=interests if plan.strategy in ("branded", "ai_trading") else None,
        )
        for query in qpack[:query_limit]:
            try:
                results.extend(fetch(query))
            except Exception:
                continue

        items.append(
            _summarize_asset(
                plan.display_label,
                country,
                results,
                strategy=plan.strategy,
                match_subject=match_subject,
                interests=interests,
                theme_terms=theme_terms,
                query_basis=query_basis,
                theme_label=theme_label,
                reference_now=ref,
                max_news_age_days=max_age,
            )
        )
        if len(items) >= max_assets:
            break

    return {
        "country": country,
        "generated_at": ref.isoformat(),
        "items": items,
    }


def fetch_hybrid_news(query: str, *, country: str | None = None) -> List[NewsResult]:
    """Fetch from Google News RSS and GDELT. Failures return partial results."""
    results: List[NewsResult] = []
    try:
        results.extend(fetch_google_news_rss(query, country=country))
    except Exception:
        pass
    try:
        results.extend(fetch_gdelt_articles(query))
    except Exception:
        pass
    return dedupe_results(results)


def fetch_google_news_rss(
    query: str,
    timeout: float = 3.0,
    *,
    country: str | None = None,
) -> List[NewsResult]:
    resp = requests.get(google_news_rss_url(query, country=country), timeout=timeout)
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    items: List[NewsResult] = []
    for item in root.findall("./channel/item")[:25]:
        title = html.unescape(item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        source_el = item.find("source")
        source = html.unescape(source_el.text or "").strip() if source_el is not None else ""
        pub_date = item.findtext("pubDate") or ""
        published_at = ""
        if pub_date:
            try:
                published_at = email.utils.parsedate_to_datetime(pub_date).isoformat()
            except (TypeError, ValueError):
                published_at = pub_date
        items.append(
            {
                "title": title,
                "snippet": "",
                "url": link,
                "source": source,
                "published_at": published_at,
            }
        )
    return items


def fetch_gdelt_articles(query: str, timeout: float = 3.0) -> List[NewsResult]:
    days = max(1, int(os.environ.get("NEWS_MAX_AGE_DAYS", "7")))
    resp = requests.get(gdelt_doc_url(query, days=days), timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    articles = data.get("articles") or []
    items: List[NewsResult] = []
    for article in articles[:15]:
        items.append(
            {
                "title": str(article.get("title", "") or "").strip(),
                "snippet": str(article.get("seendate", "") or "").strip(),
                "url": str(article.get("url", "") or "").strip(),
                "source": str(article.get("sourceCountry", "") or "").strip(),
                "published_at": str(article.get("seendate", "") or "").strip(),
            }
        )
    return items
