"""
Paper Hunter Agent
------------------
Searches strategy research sources, starting with Quantpedia.
Extracts metadata and stores raw paper records in the DB.

Usage:
    python -m pipeline.agents.paper_hunter --search "momentum equity"
    python -m pipeline.agents.paper_hunter --search "mean reversion"
    python -m pipeline.agents.paper_hunter --top 10
"""

import argparse
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from pipeline.db import init_db, log_agent_action

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

QUANTPEDIA_BASE = "https://quantpedia.com"
STRATEGIES_URL = f"{QUANTPEDIA_BASE}/strategies/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Polite delay between requests
REQUEST_DELAY = 2.0


def _session() -> requests.Session:
    """Requests session with retry + backoff."""
    s = requests.Session()
    retries = Retry(total=3, backoff_factor=2, status_forcelist=[429, 500, 502, 503])
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.headers.update(HEADERS)
    return s


_sess = _session()


@dataclass
class PaperRecord:
    source: str
    source_id: str
    url: str
    title: str
    authors: str
    abstract: str
    published_date: str
    asset_class: str
    claimed_sharpe: float | None
    claimed_cagr: float | None
    claimed_win_rate: float | None
    holding_period: str
    search_terms: str


def _normalize_asset_class(text: str) -> str:
    """Map Quantpedia market labels to our schema values."""
    t = text.lower()
    for keyword, label in [
        ("equit", "equity"), ("stock", "equity"),
        ("commodit", "commodities"), ("currenc", "fx"), ("forex", "fx"),
        ("bond", "bonds"), ("reit", "reits"), ("crypto", "crypto"),
    ]:
        if keyword in t:
            return label
    return text.strip().lower()


def fetch_strategy_listing() -> list[dict]:
    """Fetch the Quantpedia strategies listing page and extract strategy links."""
    log.info("Fetching Quantpedia strategy listing...")
    resp = _sess.get(STRATEGIES_URL, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    strategies = []

    # Try table rows first — each row has: title link, markets, rebalancing
    for row in soup.select("tr, .strategy-row, [class*='strategy']"):
        link = row.find("a", href=True)
        if not link:
            continue
        href = link["href"]
        if "/strategies/" not in href or href in ("/strategies/", STRATEGIES_URL):
            continue
        if href.startswith("/"):
            href = QUANTPEDIA_BASE + href
        title = link.get_text(strip=True)
        if not title or len(title) <= 5:
            continue

        # Extract asset class from sibling cells in the row
        row_text = row.get_text(separator=" ", strip=True)
        asset_class = ""
        for market in ["Equities", "Bonds", "Commodities", "Currencies", "REITs", "Crypto"]:
            if market in row_text:
                asset_class = _normalize_asset_class(market)
                break

        # Extract rebalancing from row
        rebalancing = ""
        for period in ["Daily", "Weekly", "Monthly", "Quarterly", "Yearly"]:
            if period in row_text:
                rebalancing = period.lower()
                break

        slug = href.rstrip("/").split("/")[-1]
        strategies.append({
            "url": href,
            "title": title,
            "slug": slug,
            "asset_class": asset_class,
            "rebalancing": rebalancing,
        })

    # Fallback: scan all links if table parse found nothing
    if not strategies:
        for link in soup.find_all("a", href=True):
            href = link["href"]
            if "/strategies/" in href and href not in ("/strategies/", STRATEGIES_URL):
                if href.startswith("/"):
                    href = QUANTPEDIA_BASE + href
                title = link.get_text(strip=True)
                if title and len(title) > 5:
                    slug = href.rstrip("/").split("/")[-1]
                    strategies.append({
                        "url": href,
                        "title": title,
                        "slug": slug,
                        "asset_class": "",
                        "rebalancing": "",
                    })

    # Deduplicate by URL
    seen = set()
    unique = []
    for s in strategies:
        if s["url"] not in seen:
            seen.add(s["url"])
            unique.append(s)

    log.info(f"Found {len(unique)} strategy links on listing page")
    return unique


def fetch_strategy_detail(url: str) -> dict:
    """Fetch a single Quantpedia strategy page and extract structured data."""
    resp = _sess.get(url, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    text = soup.get_text(separator="\n", strip=True)

    detail = {
        "abstract": "",
        "sharpe": None,
        "cagr": None,
        "max_dd": None,
        "volatility": None,
        "asset_class": "",
        "rebalancing": "",
        "markets": "",
        "trading_rules": "",
    }

    # Extract description/abstract — usually in the main content area
    desc_section = soup.find("div", class_=re.compile(r"strategy.*desc|entry-content|post-content", re.I))
    if desc_section:
        paragraphs = desc_section.find_all("p")
        detail["abstract"] = "\n".join(p.get_text(strip=True) for p in paragraphs[:5])

    if not detail["abstract"]:
        # Fallback: grab first substantial paragraphs from page text
        paras = [line for line in text.split("\n") if len(line) > 80]
        detail["abstract"] = "\n".join(paras[:5])

    # Extract performance metrics from text
    sharpe_match = re.search(r"sharpe\s*(?:ratio)?[:\s]*([0-9]+\.?[0-9]*)", text, re.I)
    if sharpe_match:
        detail["sharpe"] = float(sharpe_match.group(1))

    cagr_match = re.search(r"(?:annualized\s+return|cagr)[:\s]*([0-9]+\.?[0-9]*)%", text, re.I)
    if cagr_match:
        detail["cagr"] = float(cagr_match.group(1))

    dd_match = re.search(r"max(?:imum)?\s*drawdown[:\s]*-?([0-9]+\.?[0-9]*)%", text, re.I)
    if dd_match:
        detail["max_dd"] = float(dd_match.group(1))

    vol_match = re.search(r"(?:annualized\s+)?volatility[:\s]*([0-9]+\.?[0-9]*)%", text, re.I)
    if vol_match:
        detail["volatility"] = float(vol_match.group(1))

    # Asset class from meta tags or text
    for kw in ["equit", "stock", "commodit", "fx", "currenc", "bond", "reit", "crypto"]:
        if kw in text.lower():
            mapping = {
                "equit": "equity", "stock": "equity",
                "commodit": "commodities", "fx": "fx", "currenc": "fx",
                "bond": "bonds", "reit": "reits", "crypto": "crypto",
            }
            detail["asset_class"] = mapping.get(kw, kw)
            break

    # Rebalancing period
    reb_match = re.search(r"rebalanc(?:ing|e)\s*(?:period)?[:\s]*(daily|weekly|monthly|quarterly|yearly)", text, re.I)
    if reb_match:
        detail["rebalancing"] = reb_match.group(1).lower()

    # Trading rules section
    rules_patterns = [
        r"trading\s+rules.*?(?=fundamental|key|performance|source|backtest|\Z)",
        r"(?:entry|buy)\s*(?:rule|signal|condition).*?(?:exit|sell)\s*(?:rule|signal|condition).*?(?=\n\n|\Z)",
    ]
    for pat in rules_patterns:
        rules_match = re.search(pat, text, re.I | re.DOTALL)
        if rules_match:
            detail["trading_rules"] = rules_match.group(0)[:2000]
            break

    return detail


def filter_by_search(strategies: list[dict], search_terms: str) -> list[dict]:
    """Filter strategies by keyword match on title."""
    terms = search_terms.lower().split()
    matched = []
    for s in strategies:
        title_lower = s["title"].lower()
        if any(term in title_lower for term in terms):
            matched.append(s)
    return matched


def infer_holding_period(rebalancing: str) -> str:
    """Map rebalancing frequency to approximate holding period."""
    mapping = {
        "daily": "daily",
        "weekly": "weekly",
        "monthly": "monthly",
        "quarterly": "monthly",
        "yearly": "monthly",
    }
    return mapping.get(rebalancing, "")


def store_paper(conn: sqlite3.Connection, record: PaperRecord) -> int | None:
    """Insert a paper record, skip if duplicate. Returns paper ID or None."""
    try:
        cursor = conn.execute(
            """INSERT INTO papers
               (source, source_id, url, title, authors, abstract,
                published_date, asset_class, claimed_sharpe, claimed_cagr,
                claimed_win_rate, holding_period, search_terms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.source, record.source_id, record.url, record.title,
                record.authors, record.abstract, record.published_date,
                record.asset_class, record.claimed_sharpe, record.claimed_cagr,
                record.claimed_win_rate, record.holding_period, record.search_terms,
            ),
        )
        conn.commit()
        return cursor.lastrowid
    except sqlite3.IntegrityError:
        log.info(f"  Skipping duplicate: {record.title}")
        return None


def hunt(
    search_terms: str | None = None,
    top_n: int = 10,
    db_path: str | None = None,
) -> list[dict]:
    """
    Main entry point for Paper Hunter.

    Args:
        search_terms: Keywords to filter strategies (e.g. "momentum equity").
                      If None, returns top_n from the full listing.
        top_n: Maximum number of strategies to fetch details for.
        db_path: Optional path to SQLite DB.

    Returns:
        List of paper records stored.
    """
    conn = init_db(db_path)
    stored = []

    log_agent_action(
        conn, "paper_hunter", "search_started",
        inputs={"search_terms": search_terms, "top_n": top_n, "source": "quantpedia"},
    )

    # Step 1: Get strategy listing
    strategies = fetch_strategy_listing()

    # Step 2: Filter if search terms provided
    if search_terms:
        strategies = filter_by_search(strategies, search_terms)
        log.info(f"Filtered to {len(strategies)} strategies matching '{search_terms}'")

    # Step 3: Limit to top_n
    strategies = strategies[:top_n]

    if not strategies:
        log.warning("No strategies found matching criteria")
        log_agent_action(
            conn, "paper_hunter", "search_completed",
            outputs={"count": 0, "reason": "no matches"},
        )
        return []

    # Step 4: Fetch detail for each strategy
    for i, strat in enumerate(strategies):
        log.info(f"[{i+1}/{len(strategies)}] Fetching: {strat['title']}")

        try:
            detail = fetch_strategy_detail(strat["url"])
        except Exception as e:
            log.error(f"  Failed to fetch detail: {e}")
            time.sleep(REQUEST_DELAY)
            continue

        # Extract source_id from slug
        source_id = strat["slug"]

        # Prefer listing-level metadata (accurate), fall back to detail-page guess
        asset_class = strat.get("asset_class") or detail.get("asset_class", "")
        rebalancing = strat.get("rebalancing") or detail.get("rebalancing", "")

        record = PaperRecord(
            source="quantpedia",
            source_id=source_id,
            url=strat["url"],
            title=strat["title"],
            authors="",  # Quantpedia doesn't list paper authors on strategy pages
            abstract=detail["abstract"][:5000],
            published_date="",
            asset_class=asset_class,
            claimed_sharpe=detail.get("sharpe"),
            claimed_cagr=detail.get("cagr"),
            claimed_win_rate=None,
            holding_period=infer_holding_period(rebalancing),
            search_terms=search_terms or "top_listing",
        )

        paper_id = store_paper(conn, record)
        if paper_id:
            stored.append({"id": paper_id, **asdict(record)})
            log.info(f"  Stored: id={paper_id}, sharpe={record.claimed_sharpe}, "
                     f"cagr={record.claimed_cagr}, asset={record.asset_class}")

            log_agent_action(
                conn, "paper_hunter", "paper_stored",
                inputs={"url": strat["url"]},
                outputs={"paper_id": paper_id, "title": record.title,
                         "sharpe": record.claimed_sharpe, "cagr": record.claimed_cagr},
            )

        time.sleep(REQUEST_DELAY)

    log_agent_action(
        conn, "paper_hunter", "search_completed",
        outputs={"stored_count": len(stored), "titles": [s["title"] for s in stored]},
    )

    log.info(f"\nDone. Stored {len(stored)} papers.")
    return stored


def main():
    parser = argparse.ArgumentParser(description="Paper Hunter — find trading strategies")
    parser.add_argument("--search", type=str, default=None,
                        help="Search terms (e.g. 'momentum equity')")
    parser.add_argument("--top", type=int, default=10,
                        help="Max strategies to fetch (default: 10)")
    parser.add_argument("--db", type=str, default=None,
                        help="Path to SQLite database")
    args = parser.parse_args()

    results = hunt(search_terms=args.search, top_n=args.top, db_path=args.db)

    if results:
        print(f"\n{'='*60}")
        print(f"PAPER HUNTER RESULTS — {len(results)} strategies found")
        print(f"{'='*60}")
        for r in results:
            sharpe = f"{r['claimed_sharpe']:.2f}" if r['claimed_sharpe'] else "N/A"
            cagr = f"{r['claimed_cagr']:.1f}%" if r['claimed_cagr'] else "N/A"
            print(f"  [{r['id']}] {r['title']}")
            print(f"      Sharpe: {sharpe} | CAGR: {cagr} | Asset: {r['asset_class']}")
            print()


if __name__ == "__main__":
    main()
