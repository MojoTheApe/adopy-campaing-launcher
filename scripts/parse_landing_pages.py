"""Парсинг лендинг-пейджей из knowledge_base.json → knowledge_base/landing_pages.md.

Для каждого оффера читает список lp_urls из config/knowledge_base.json, затем для каждого URL:
  - HTTP GET с таймаутом 10 сек и браузерным User-Agent
  - 404 / ошибка подключения → помечает [BROKEN] и добавляет в сводку
  - 200 → извлекает: <title>, <h1>, <meta description>, первый CTA (button/a.cta)
  - Перезаписывает knowledge_base/landing_pages.md

Использование:
    python scripts/parse_landing_pages.py
    python scripts/parse_landing_pages.py --timeout 15
    python scripts/parse_landing_pages.py --delay 0.5
    python scripts/parse_landing_pages.py --dry-run   # только вывод, без записи файла
    python scripts/parse_landing_pages.py --offer "AI Trader"  # только один оффер
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

_PROJ_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

_KB_JSON = _PROJ_ROOT / "config" / "knowledge_base.json"
_KB_LP_MD = _PROJ_ROOT / "knowledge_base" / "landing_pages.md"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


# ── KB helpers ─────────────────────────────────────────────────────────────────

def load_kb_json() -> Dict[str, Any]:
    if not _KB_JSON.is_file():
        return {}
    try:
        with open(_KB_JSON, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _get_lp_urls(entry: Dict[str, Any]) -> List[str]:
    """Get list of LP URLs from an offer entry (supports both lp_urls list and legacy lp_url string)."""
    if "lp_urls" in entry and isinstance(entry["lp_urls"], list):
        return [u for u in entry["lp_urls"] if u and isinstance(u, str)]
    if "lp_url" in entry and entry["lp_url"]:
        return [entry["lp_url"]]
    return []


# ── URL fetching ───────────────────────────────────────────────────────────────

def _safe_get(url: str, timeout: int):
    """Fetch URL. Returns (response, error_message)."""
    try:
        import requests
    except ImportError:
        print("[ERROR] requests не установлен: pip install requests", file=sys.stderr)
        sys.exit(1)

    headers = {"User-Agent": _USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        return resp, None
    except requests.exceptions.ConnectionError as e:
        return None, f"Connection error: {e}"
    except requests.exceptions.Timeout:
        return None, f"Timeout after {timeout}s"
    except requests.exceptions.RequestException as e:
        return None, str(e)


# ── HTML parsing ───────────────────────────────────────────────────────────────

def _text(tag) -> str:
    if tag is None:
        return ""
    return tag.get_text(separator=" ", strip=True)


def extract_page_info(html: str) -> Dict[str, str]:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("[ERROR] beautifulsoup4 не установлен: pip install beautifulsoup4", file=sys.stderr)
        sys.exit(1)

    soup = BeautifulSoup(html, "html.parser")

    title = _text(soup.find("title"))
    h1 = _text(soup.find("h1"))

    meta_desc = ""
    meta_tag = soup.find("meta", attrs={"name": "description"})
    if meta_tag and meta_tag.get("content"):
        meta_desc = str(meta_tag["content"]).strip()

    cta_text = ""
    cta_keywords = ("cta", "btn", "button", "register", "signup", "sign-up", "start", "get-started")

    for btn in soup.find_all("button"):
        btn_text = _text(btn).strip()
        if btn_text:
            cta_text = btn_text
            break

    if not cta_text:
        for a in soup.find_all("a"):
            classes = " ".join(a.get("class", [])).lower()
            if any(kw in classes for kw in cta_keywords):
                a_text = _text(a).strip()
                if a_text:
                    cta_text = a_text
                    break

    if not cta_text:
        submit = soup.find("input", attrs={"type": "submit"})
        if submit and submit.get("value"):
            cta_text = str(submit["value"]).strip()

    return {
        "title": title[:200] if title else "",
        "h1": h1[:200] if h1 else "",
        "meta_description": meta_desc[:300] if meta_desc else "",
        "cta": cta_text[:200] if cta_text else "",
    }


# ── LP result ──────────────────────────────────────────────────────────────────

class LPResult:
    def __init__(
        self,
        offer_name: str,
        url: str,
        status: int,
        error: str,
        info: Optional[Dict[str, str]],
    ):
        self.offer_name = offer_name
        self.url = url
        self.status = status
        self.error = error
        self.info = info or {}

    @property
    def is_ok(self) -> bool:
        return self.status == 200

    @property
    def is_broken(self) -> bool:
        return bool(self.url) and not self.is_ok


# ── Fetching loop ──────────────────────────────────────────────────────────────

def fetch_all_lps(
    offers: Dict[str, Any],
    timeout: int,
    delay: float,
    only_offer: Optional[str] = None,
) -> List[LPResult]:
    results: List[LPResult] = []

    for offer_name, entry in offers.items():
        if not isinstance(entry, dict):
            continue
        if only_offer and offer_name.lower() != only_offer.lower():
            continue

        urls = _get_lp_urls(entry)

        if not urls:
            print(f"  [SKIP] {offer_name}: no LP URLs configured")
            results.append(LPResult(offer_name, "", 0, "no lp_urls configured", None))
            continue

        for url in urls:
            parsed = urlparse(url)
            if not parsed.scheme or not parsed.netloc:
                print(f"  [SKIP] {offer_name}: invalid URL: {url!r}", file=sys.stderr)
                results.append(LPResult(offer_name, url, 0, "invalid URL format", None))
                continue

            print(f"  Fetching [{offer_name}] {url}")
            resp, err = _safe_get(url, timeout)

            if err or resp is None:
                print(f"  [BROKEN] {offer_name}: {err}", file=sys.stderr)
                results.append(LPResult(offer_name, url, 0, err or "unknown error", None))
            elif resp.status_code != 200:
                msg = f"HTTP {resp.status_code}"
                print(f"  [BROKEN] {offer_name}: {msg}", file=sys.stderr)
                results.append(LPResult(offer_name, url, resp.status_code, msg, None))
            else:
                info = extract_page_info(resp.text)
                results.append(LPResult(offer_name, url, 200, "", info))
                title_short = info['title'][:60] if info['title'] else "(no title)"
                print(f"  [OK]     [{offer_name}] title={title_short!r}")

            if delay > 0:
                time.sleep(delay)

    return results


# ── Markdown generation ────────────────────────────────────────────────────────

def _md_offer_section(offer_name: str, url_results: List[LPResult]) -> str:
    """Generate a markdown section for one offer (may have multiple URLs)."""
    lines = [f"## {offer_name}", ""]

    if not url_results or (len(url_results) == 1 and not url_results[0].url):
        lines.append("_LP URLs не настроены_")
        lines.extend(["", "---", ""])
        return "\n".join(lines)

    for r in url_results:
        short_url = r.url[:80] + "..." if len(r.url) > 80 else r.url
        lines.append(f"### `{short_url}`")
        lines.append("")
        if r.is_broken:
            lines.append(f"- **Статус:** BROKEN — {r.error}")
            lines.append("- **Действие:** Замени URL в Excel и запусти sync_offers + parse_lps повторно")
        else:
            lines.append(f"- **Статус:** OK (HTTP {r.status})")
            info = r.info
            if info.get("title"):
                lines.append(f"- **Title:** {info['title']}")
            if info.get("h1"):
                lines.append(f"- **H1:** {info['h1']}")
            if info.get("meta_description"):
                lines.append(f"- **Meta description:** {info['meta_description']}")
            if info.get("cta"):
                lines.append(f"- **CTA:** {info['cta']}")
        lines.append("")

    lines.extend(["---", ""])
    return "\n".join(lines)


def generate_lp_md(results: List[LPResult]) -> str:
    from datetime import datetime
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    ok_count = sum(1 for r in results if r.is_ok)
    broken_count = sum(1 for r in results if r.is_broken)
    skip_count = sum(1 for r in results if not r.url)

    broken_list = [r for r in results if r.is_broken]

    header_lines = [
        "# Landing Pages",
        "",
        "Авто-генерируется скриптом `scripts/parse_landing_pages.py`.",
        f"_Последний запуск: {ts}_",
        "",
        f"**Сводка:** OK: {ok_count} | Broken: {broken_count} | Без URL: {skip_count}",
        "",
    ]

    if broken_list:
        header_lines.append("## ⚠ Broken URLs (требуют замены)")
        header_lines.append("")
        for r in broken_list:
            short_url = r.url[:80] + "..." if len(r.url) > 80 else r.url
            header_lines.append(f"- **{r.offer_name}**: `{short_url}` — {r.error}")
        header_lines.extend(["", "---", ""])

    header = "\n".join(header_lines) + "\n"

    # Group results by offer
    offer_map: Dict[str, List[LPResult]] = {}
    for r in results:
        offer_map.setdefault(r.offer_name, []).append(r)

    body = "".join(_md_offer_section(name, rs) for name, rs in offer_map.items())
    return header + body


# ── Main ───────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Parse landing pages from knowledge_base.json → landing_pages.md"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        help="HTTP request timeout in seconds (default: 10)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Delay between requests in seconds (default: 0.5)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch pages and print results without writing landing_pages.md",
    )
    parser.add_argument(
        "--offer",
        default=None,
        help="Process only this offer name (for testing)",
    )
    return parser.parse_args()


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    args = _parse_args()

    kb = load_kb_json()
    offers = kb.get("offers", {})

    if not offers:
        print("[parse_lps] No offers found in knowledge_base.json")
        return

    total_urls = sum(len(_get_lp_urls(e)) for e in offers.values() if isinstance(e, dict))
    scope = f"offer={args.offer!r}" if args.offer else f"all {len(offers)} offers, {total_urls} URLs"
    print(f"[parse_lps] Processing {scope}...")
    print()

    results = fetch_all_lps(offers, timeout=args.timeout, delay=args.delay, only_offer=args.offer)

    ok = sum(1 for r in results if r.is_ok)
    broken = sum(1 for r in results if r.is_broken)
    skip = sum(1 for r in results if not r.url)

    print(f"\n--- Summary ---")
    print(f"  OK:       {ok}")
    print(f"  Broken:   {broken}")
    print(f"  No URL:   {skip}")

    if broken > 0:
        print(f"\n[!] BROKEN URLs ({broken}):", file=sys.stderr)
        for r in results:
            if r.is_broken:
                print(f"  {r.offer_name}: {r.url} — {r.error}", file=sys.stderr)

    if args.dry_run:
        print("\n[dry-run] landing_pages.md not written.")
        return

    _KB_LP_MD.parent.mkdir(parents=True, exist_ok=True)
    md = generate_lp_md(results)
    with open(_KB_LP_MD, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"\n[parse_lps] Wrote knowledge_base/landing_pages.md")


if __name__ == "__main__":
    main()
