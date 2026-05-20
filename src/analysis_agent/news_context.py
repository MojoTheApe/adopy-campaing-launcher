"""Build fresh asset-news context for Morning Report country sections.

The module keeps source fetching separate from report aggregation. Tests can
pass a fake fetcher; production uses a small hybrid of Google News RSS and
GDELT, with conservative summaries based on retrieved titles/snippets.
"""
from __future__ import annotations

import email.utils
import html
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
import xml.etree.ElementTree as ET

import requests

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


def canonical_news_asset(asset: str) -> tuple[str, str, str]:
    """Return (dedupe_key, display_label, news_query_subject).

    Multiple Excel spellings map to one dedupe_key so news runs once.
    AI-trader variants use thematic queries around ``AI trading``.
    """
    raw = str(asset or "").strip()
    if not raw:
        return ("", "", "")
    key = normalize_asset_key(raw)
    if _is_ai_trading_family(key):
        return (_AI_TRADING_DEDUPE_KEY, "AI Trader", "AI trading")
    return (key, raw, raw)


def merge_assets_for_news(assets: Iterable[str]) -> List[tuple[str, str, bool]]:
    """Stable unique list of (display_label, news_query_subject, ai_trading_pack)."""
    seen: set[str] = set()
    out: List[tuple[str, str, bool]] = []
    for a in assets:
        dedupe_key, display, subject = canonical_news_asset(str(a or ""))
        if not dedupe_key:
            continue
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        ai_pack = dedupe_key == _AI_TRADING_DEDUPE_KEY
        out.append((display, subject, ai_pack))
    return out


def build_asset_queries(news_subject: str, country: str, *, ai_trading: bool = False) -> List[str]:
    """Return the small hybrid query bundle for one news_subject/country pair."""
    subject_s = str(news_subject or "").strip()
    country_s = str(country or "").strip()
    if ai_trading:
        queries = [
            f"AI trading artificial intelligence {country_s} news last 7 days",
            f"retail AI trading apps brokers {country_s} regulation news",
            f"AI trading investor sentiment algorithmic trading news",
        ]
    else:
        queries = [
            f"{subject_s} {country_s} news last 7 days",
            f"{subject_s} stock shares earnings partnership regulation {country_s}",
            f"{subject_s} investor relations press release",
        ]
    local_terms = _LOCAL_NEWS_TERMS.get(country_s.lower())
    if local_terms:
        queries.append(f"{subject_s} {country_s} {local_terms}")
    return queries


def google_news_rss_url(query: str) -> str:
    params = urlencode({"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
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
    # GDELT doc API seendate: YYYYMMDDHHMMSS
    if s.isdigit() and len(s) == 14:
        try:
            return datetime.strptime(s, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    # Date-only (tests / loose callers)
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


def _score_result(result: NewsResult, match_subject: str, country: str) -> int:
    text = f"{result.get('title', '')} {result.get('snippet', '')}".lower()
    source = str(result.get("source", "")).lower()
    score = 0
    subj = str(match_subject or "").strip().lower()
    if subj:
        if subj in text:
            score += 5
        else:
            for word in subj.split():
                if len(word) > 2 and word in text:
                    score += 2
        if subj == "ai trading" and ("ai" in text or "artificial intelligence" in text) and (
            "trad" in text or "broker" in text or "invest" in text
        ):
            score += 2
    if country.lower() in text:
        score += 2
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


def _fallback_item(asset: str) -> Dict[str, Any]:
    return {
        "asset": asset,
        "signal": "no strong fresh news found",
        "summary": "No reliable recent asset news found.",
        "headline_angle": "Use evergreen angles from working combos rather than newsjacking.",
        "confidence": "low",
        "freshness_days": None,
    }


def _rank_fresh_news_results(
    results: List[NewsResult],
    *,
    match_subject: str,
    country: str,
    reference_now: datetime,
    max_age_days: float,
) -> List[NewsResult]:
    """Keep deduped hits younger than ``max_age_days``; prefer relevance then recency."""
    deduped = dedupe_results(results)
    scored: List[tuple[NewsResult, float | None]] = []
    for r in deduped:
        pub = parse_news_datetime(r.get("published_at"))
        if pub is None:
            scored.append((r, None))
            continue
        age = _age_days_since(pub, reference_now)
        if age > max_age_days:
            continue
        scored.append((r, age))
    if not scored:
        return []

    def sort_key(row: tuple[NewsResult, float | None]) -> tuple[int, int, float]:
        r, age = row
        score = _score_result(r, match_subject, country)
        if age is None:
            # Undated items lose ties against explicitly fresh dated rows.
            return (1, -score, 0.0)
        return (0, -score, age)

    scored.sort(key=sort_key)
    return [pair[0] for pair in scored]


def _summarize_asset(
    asset_display: str,
    country: str,
    results: List[NewsResult],
    *,
    match_subject: str,
    ai_trading: bool,
    reference_now: datetime,
    max_news_age_days: float,
) -> Dict[str, Any]:
    ranked = _rank_fresh_news_results(
        results,
        match_subject=match_subject,
        country=country,
        reference_now=reference_now,
        max_age_days=max_news_age_days,
    )
    if not ranked:
        return _fallback_item(asset_display)

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
    if ai_trading:
        angle = (
            f"Tie {asset_display} headlines to AI-assisted trading trends and platform news "
            f"in {country} — stay factual, avoid hype."
        )
    else:
        angle = f"Connect {asset_display} to {signal} in {country} with a cautious, timely hook."
    return {
        "asset": asset_display,
        "signal": signal,
        "summary": text[:240],
        "headline_angle": angle,
        "confidence": "medium" if sources else "low",
        "freshness_days": freshness_days,
    }


def build_news_context(
    country: str,
    assets: Iterable[str],
    *,
    fetcher: NewsFetcher | None = None,
    max_assets: int = 3,
    max_queries_per_asset: int = 2,
    reference_now: datetime | None = None,
    max_news_age_days: int | None = None,
) -> Dict[str, Any]:
    """Fetch and normalize news context for assets in one country.

    Rows older than ``max_news_age_days`` (env ``NEWS_MAX_AGE_DAYS``, default 7)
    are dropped when ``published_at`` parses; undated rows are kept but ranked
    after dated fresh items.
    """
    fetch = fetcher or fetch_hybrid_news
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
    items: List[Dict[str, Any]] = []
    merged = merge_assets_for_news(assets)
    for display_label, news_subject, ai_trading in merged:
        results: List[NewsResult] = []
        qpack = build_asset_queries(news_subject, country, ai_trading=ai_trading)
        for query in qpack[:max_queries_per_asset]:
            try:
                results.extend(fetch(query))
            except Exception:
                continue
        items.append(
            _summarize_asset(
                display_label,
                country,
                results,
                match_subject=news_subject,
                ai_trading=ai_trading,
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


def fetch_hybrid_news(query: str) -> List[NewsResult]:
    """Fetch from Google News RSS and GDELT. Failures return partial results."""
    results: List[NewsResult] = []
    for fetch in (fetch_google_news_rss, fetch_gdelt_articles):
        try:
            results.extend(fetch(query))
        except Exception:
            continue
    return dedupe_results(results)


def fetch_google_news_rss(query: str, timeout: float = 3.0) -> List[NewsResult]:
    resp = requests.get(google_news_rss_url(query), timeout=timeout)
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
