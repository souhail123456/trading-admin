"""
Paper Reader Agent
------------------
Takes papers from the DB (found by Paper Hunter), fetches the full strategy page,
and uses Groq/Llama 3.3 70B to extract structured strategy specifications.

Usage:
    python -m pipeline.agents.paper_reader                # process all unread papers
    python -m pipeline.agents.paper_reader --paper-id 3   # process specific paper
    python -m pipeline.agents.paper_reader --dry-run      # extract but don't store
"""

import argparse
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, asdict, field

import requests
from bs4 import BeautifulSoup
from groq import Groq
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from pipeline.db import init_db, log_agent_action

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
}

REQUEST_DELAY = 2.0
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

EXTRACTION_PROMPT = """You are a quantitative finance research assistant. Given the text of a trading strategy page, extract a structured specification.

Return ONLY valid JSON with exactly these fields:
{
  "name": "short human-readable strategy name",
  "entry_rule": "exact entry rule — when to buy/go long. Be specific: what indicator, what threshold, what timeframe. Write as pseudocode if possible.",
  "exit_rule": "exact exit rule — when to sell/close. Same level of detail.",
  "asset_universe": "what instruments this trades (e.g. 'S&P 500 stocks', 'G10 currencies', 'commodity futures')",
  "data_requirements": "what data is needed (e.g. 'daily OHLCV', 'fundamental data', 'options chain')",
  "position_sizing": "how positions are sized (e.g. 'equal weight', 'market-cap weighted', 'risk parity')",
  "holding_period": "typical holding period (e.g. 'daily', 'weekly', 'monthly', '6 months')",
  "parameters": {"key_param_1": "value", "key_param_2": "value"},
  "claimed_sharpe": null,
  "claimed_cagr": null,
  "claimed_max_dd": null,
  "test_period": "e.g. '1962-2002'",
  "caveats": "any warnings, declining performance, data issues, or limitations"
}

Rules:
- For entry_rule and exit_rule: be as specific and actionable as possible. If the page says "buy winners and sell losers", specify WHAT makes a winner (e.g. "top decile by 12-month return, excluding most recent month").
- For parameters: extract all numerical values that define the strategy (lookback periods, thresholds, number of stocks, rebalance frequency).
- For claimed_sharpe/cagr/max_dd: extract as numbers (floats). Use null if not stated.
- For caveats: note any mentions of declining returns, transaction costs, capacity constraints, or regime dependence.
- Do NOT invent information. If something isn't on the page, say "not stated".

Strategy page text:
"""


def _session() -> requests.Session:
    s = requests.Session()
    retries = Retry(total=3, backoff_factor=2, status_forcelist=[429, 500, 502, 503])
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.headers.update(HEADERS)
    return s


_sess = _session()


@dataclass
class StrategySpec:
    """Structured strategy specification extracted from a paper."""
    paper_id: int
    name: str
    entry_rule: str
    exit_rule: str
    asset_universe: str
    data_requirements: str
    position_sizing: str
    holding_period: str
    parameters: dict = field(default_factory=dict)
    claimed_sharpe: float | None = None
    claimed_cagr: float | None = None
    claimed_max_dd: float | None = None
    test_period: str = ""
    caveats: str = ""


# ---------------------------------------------------------------------------
# Page fetching
# ---------------------------------------------------------------------------

def fetch_page_text(url: str) -> str:
    """Fetch a strategy page and return cleaned text."""
    resp = _sess.get(url, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()

    return soup.get_text(separator="\n", strip=True)


# ---------------------------------------------------------------------------
# LLM extraction via Groq
# ---------------------------------------------------------------------------

def extract_with_llm(text: str, title: str) -> dict:
    """Send page text to Groq/Llama and get structured JSON back."""
    client = Groq()

    # Truncate to fit context window (~6K tokens of text is safe)
    truncated = text[:8000]

    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {
                "role": "system",
                "content": "You are a quantitative finance expert. Extract trading strategy specifications from research pages. Return ONLY valid JSON, no markdown fences, no explanation."
            },
            {
                "role": "user",
                "content": f"{EXTRACTION_PROMPT}\n\nTitle: {title}\n\n{truncated}"
            }
        ],
        temperature=0.1,
        max_tokens=2000,
    )

    raw = response.choices[0].message.content.strip()

    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.error(f"LLM returned invalid JSON. Raw output:\n{raw[:500]}")
        raise


# ---------------------------------------------------------------------------
# Main extraction pipeline
# ---------------------------------------------------------------------------

def read_paper(paper: dict) -> StrategySpec:
    """
    Read a paper's strategy page, send to LLM, extract StrategySpec.
    """
    url = paper["url"]
    log.info(f"Reading paper [{paper['id']}]: {paper['title']}")

    text = fetch_page_text(url)
    extracted = extract_with_llm(text, paper["title"])

    # Build spec, preferring LLM output but falling back to paper-level data
    spec = StrategySpec(
        paper_id=paper["id"],
        name=extracted.get("name", paper["title"]),
        entry_rule=extracted.get("entry_rule", "not stated"),
        exit_rule=extracted.get("exit_rule", "not stated"),
        asset_universe=extracted.get("asset_universe", "not stated"),
        data_requirements=extracted.get("data_requirements", "OHLCV price data"),
        position_sizing=extracted.get("position_sizing", "equal weight"),
        holding_period=extracted.get("holding_period", paper.get("holding_period", "")),
        parameters=extracted.get("parameters", {}),
        claimed_sharpe=_to_float(extracted.get("claimed_sharpe")) or paper.get("claimed_sharpe"),
        claimed_cagr=_to_float(extracted.get("claimed_cagr")) or paper.get("claimed_cagr"),
        claimed_max_dd=_to_float(extracted.get("claimed_max_dd")),
        test_period=extracted.get("test_period", ""),
        caveats=extracted.get("caveats", ""),
    )

    return spec


def _to_float(val) -> float | None:
    """Safely convert a value to float, return None on failure."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def store_strategy(conn: sqlite3.Connection, spec: StrategySpec) -> int:
    """Insert a strategy spec into the strategies table. Returns strategy ID."""
    cursor = conn.execute(
        """INSERT INTO strategies
           (paper_id, name, entry_rule, exit_rule, asset_universe,
            data_requirements, position_sizing, holding_period, parameters,
            claimed_sharpe, claimed_cagr, claimed_max_dd, test_period, caveats)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            spec.paper_id, spec.name, spec.entry_rule, spec.exit_rule,
            spec.asset_universe, spec.data_requirements, spec.position_sizing,
            spec.holding_period, json.dumps(spec.parameters),
            spec.claimed_sharpe, spec.claimed_cagr, spec.claimed_max_dd,
            spec.test_period, spec.caveats,
        ),
    )
    conn.commit()
    return cursor.lastrowid


def get_unread_papers(conn: sqlite3.Connection) -> list[dict]:
    """Get papers that don't yet have a strategy entry."""
    rows = conn.execute(
        """SELECT p.* FROM papers p
           LEFT JOIN strategies s ON s.paper_id = p.id
           WHERE s.id IS NULL
           ORDER BY p.id"""
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def read_all(
    paper_id: int | None = None,
    dry_run: bool = False,
    db_path: str | None = None,
) -> list[dict]:
    """
    Process papers and extract strategy specs via LLM.
    """
    conn = init_db(db_path)
    results = []

    if paper_id:
        row = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
        if not row:
            log.error(f"Paper {paper_id} not found")
            return []
        papers = [dict(row)]
    else:
        papers = get_unread_papers(conn)

    if not papers:
        log.info("No unread papers to process.")
        return []

    log.info(f"Processing {len(papers)} paper(s) with Groq/{GROQ_MODEL}...")

    log_agent_action(
        conn, "paper_reader", "read_started",
        inputs={"paper_ids": [p["id"] for p in papers], "model": GROQ_MODEL, "dry_run": dry_run},
    )

    for paper in papers:
        try:
            spec = read_paper(paper)
        except Exception as e:
            log.error(f"  Failed to read paper [{paper['id']}]: {e}")
            log_agent_action(
                conn, "paper_reader", "read_failed",
                inputs={"paper_id": paper["id"]},
                outputs={"error": str(e)},
            )
            time.sleep(REQUEST_DELAY)
            continue

        spec_dict = asdict(spec)

        if dry_run:
            log.info(f"  [DRY RUN] Extracted: {spec.name}")
            _print_spec(spec)
        else:
            strategy_id = store_strategy(conn, spec)
            spec_dict["strategy_id"] = strategy_id
            log.info(f"  Stored strategy [{strategy_id}]: {spec.name}")

            log_agent_action(
                conn, "paper_reader", "strategy_extracted",
                inputs={"paper_id": paper["id"]},
                outputs={
                    "strategy_id": strategy_id,
                    "name": spec.name,
                    "entry_rule_preview": spec.entry_rule[:100],
                    "exit_rule_preview": spec.exit_rule[:100],
                    "param_count": len(spec.parameters),
                    "sharpe": spec.claimed_sharpe,
                },
                reasoning=f"LLM extraction via {GROQ_MODEL} from {paper['url']}",
                strategy_id=strategy_id,
            )

        results.append(spec_dict)
        time.sleep(REQUEST_DELAY)

    log_agent_action(
        conn, "paper_reader", "read_completed",
        outputs={"count": len(results)},
    )

    return results


def _print_spec(spec: StrategySpec) -> None:
    """Pretty-print a strategy spec."""
    print(f"\n{'─'*60}")
    print(f"  {spec.name}")
    print(f"{'─'*60}")
    print(f"  Entry: {spec.entry_rule}")
    print(f"  Exit:  {spec.exit_rule}")
    print(f"  Universe: {spec.asset_universe}")
    print(f"  Data: {spec.data_requirements}")
    print(f"  Sizing: {spec.position_sizing}")
    print(f"  Holding: {spec.holding_period}")
    print(f"  Params: {json.dumps(spec.parameters, indent=2)}")
    print(f"  Sharpe: {spec.claimed_sharpe}")
    print(f"  CAGR: {spec.claimed_cagr}")
    print(f"  Max DD: {spec.claimed_max_dd}")
    print(f"  Period: {spec.test_period}")
    print(f"  Caveats: {spec.caveats}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Paper Reader — LLM-powered strategy extraction")
    parser.add_argument("--paper-id", type=int, default=None,
                        help="Process a specific paper by ID")
    parser.add_argument("--dry-run", action="store_true",
                        help="Extract and print without storing")
    parser.add_argument("--db", type=str, default=None,
                        help="Path to SQLite database")
    args = parser.parse_args()

    results = read_all(paper_id=args.paper_id, dry_run=args.dry_run, db_path=args.db)

    if results:
        print(f"\n{'='*60}")
        print(f"PAPER READER — {len(results)} strategies extracted")
        print(f"{'='*60}")
        for r in results:
            sid = r.get("strategy_id", "?")
            print(f"  [{sid}] {r['name']}")
            print(f"      Entry: {r['entry_rule'][:120]}")
            print(f"      Exit:  {r['exit_rule'][:120]}")
            print(f"      Sharpe: {r.get('claimed_sharpe')} | Params: {len(r.get('parameters', {}))}")
            print()


if __name__ == "__main__":
    main()
