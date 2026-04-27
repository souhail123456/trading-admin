"""
Strategy Cataloger Agent
------------------------
Deduplicates strategies, ranks candidates by quality criteria, and stores
the ranked list in the DB ready for Phase 2 backtesting.

Scoring dimensions (0-1 each):
  - Rule clarity: how precisely defined are entry/exit rules?
  - Data accessibility: can we get the data with free APIs (yfinance, Alpaca)?
  - Claimed Sharpe: normalized across candidates
  - Recency: how recently was the strategy validated?

Uses Groq/Llama for deduplication judgment (are two strategies the same idea?).

Usage:
    python -m pipeline.agents.cataloger              # rank all candidates
    python -m pipeline.agents.cataloger --dry-run    # score but don't store
"""

import argparse
import json
import logging
import os
import sqlite3
from dataclasses import dataclass

from groq import Groq

from pipeline.db import init_db, log_agent_action

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

GROQ_MODEL = "llama-3.3-70b-versatile"

# Scoring weights
WEIGHTS = {
    "rule_clarity": 0.35,
    "data_access": 0.25,
    "claimed_sharpe": 0.20,
    "recency": 0.20,
}

# Data accessibility: asset universes we can easily get via yfinance/Alpaca
EASY_DATA = ["us equit", "s&p", "nyse", "nasdaq", "amex", "sector", "etf", "us stock"]
MEDIUM_DATA = ["country", "international", "index", "commodit"]
HARD_DATA = ["currenc", "fx", "bond", "option", "fundamental"]


@dataclass
class StrategyScore:
    strategy_id: int
    rule_clarity: float
    data_access: float
    claimed_sharpe_score: float
    recency_score: float
    composite_score: float
    notes: str


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def check_duplicates(conn: sqlite3.Connection, strategies: list[dict]) -> list[tuple[int, int, str]]:
    """
    Compare strategies pairwise using LLM to detect duplicates.
    Returns list of (kept_id, duplicate_id, reason).
    """
    if len(strategies) < 2:
        return []

    client = Groq()
    duplicates = []

    # Build summary of all strategies for a single LLM call
    summaries = []
    for s in strategies:
        summaries.append(f"[{s['id']}] {s['name']}: Entry={s['entry_rule'][:150]}, Exit={s['exit_rule'][:150]}, Universe={s['asset_universe']}")

    prompt = f"""You are a quant researcher. I have {len(strategies)} trading strategies.
Identify any DUPLICATES — strategies that are essentially the same idea applied to the same asset class.

Strategies that are the same concept but applied to DIFFERENT asset classes (e.g. momentum in stocks vs momentum in currencies) are NOT duplicates.

Strategies:
{chr(10).join(summaries)}

Return ONLY valid JSON — a list of duplicates found. Each entry:
{{"kept_id": <id of the better-specified one>, "duplicate_id": <id of the duplicate>, "reason": "why they're the same"}}

If no duplicates, return: []"""

    try:
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": "Return only valid JSON, no markdown fences."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=500,
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

        results = json.loads(raw)
        for r in results:
            duplicates.append((r["kept_id"], r["duplicate_id"], r["reason"]))
    except Exception as e:
        log.warning(f"Dedup LLM call failed: {e} — skipping deduplication")

    return duplicates


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_rule_clarity(strategy: dict) -> float:
    """Score how precisely the entry/exit rules are defined."""
    score = 0.0
    entry = strategy["entry_rule"].lower()
    exit_rule = strategy["exit_rule"].lower()

    # Does entry rule have specific numbers/thresholds?
    if any(c.isdigit() for c in strategy["entry_rule"]):
        score += 0.3
    # Does it mention specific indicators or conditions?
    if any(kw in entry for kw in ["month", "day", "week", "top", "bottom", "percentile", "decile"]):
        score += 0.2
    # Is it more than a vague one-liner?
    if len(strategy["entry_rule"]) > 50:
        score += 0.1
    # Does exit rule exist and differ from entry?
    if exit_rule and "not stated" not in exit_rule and "not explicitly" not in exit_rule:
        score += 0.2
    if any(c.isdigit() for c in strategy["exit_rule"]):
        score += 0.1
    # Are parameters extracted?
    params = json.loads(strategy["parameters"]) if isinstance(strategy["parameters"], str) else strategy["parameters"]
    if len(params) >= 2:
        score += 0.1

    return min(score, 1.0)


def score_data_accessibility(strategy: dict) -> float:
    """Score how easy it is to get the required data with free APIs."""
    universe = strategy["asset_universe"].lower()
    data_reqs = strategy.get("data_requirements", "").lower()

    # Check universe accessibility
    if any(kw in universe for kw in EASY_DATA):
        base = 0.8
    elif any(kw in universe for kw in MEDIUM_DATA):
        base = 0.5
    elif any(kw in universe for kw in HARD_DATA):
        base = 0.3
    else:
        base = 0.4

    # Penalty for needing non-OHLCV data
    if "fundamental" in data_reqs:
        base -= 0.15
    if "option" in data_reqs:
        base -= 0.2

    # Bonus for simple OHLCV-only
    if "ohlcv" in data_reqs and "fundamental" not in data_reqs:
        base += 0.1

    return max(0.0, min(base, 1.0))


def score_sharpe(strategy: dict, all_sharpes: list[float]) -> float:
    """Normalize claimed Sharpe against the candidate set."""
    sharpe = strategy.get("claimed_sharpe")
    if sharpe is None or not all_sharpes:
        return 0.3  # neutral score for unknown

    max_s = max(all_sharpes) if all_sharpes else 1.0
    if max_s == 0:
        return 0.5
    return min(sharpe / max(max_s, 0.01), 1.0)


def score_recency(strategy: dict) -> float:
    """Score based on how recently the strategy was validated."""
    period = strategy.get("test_period", "")
    if not period:
        return 0.3

    # Extract end year
    parts = period.replace("–", "-").split("-")
    try:
        end_year = int(parts[-1].strip()[:4])
    except (ValueError, IndexError):
        return 0.3

    if end_year >= 2020:
        return 1.0
    elif end_year >= 2015:
        return 0.8
    elif end_year >= 2010:
        return 0.6
    elif end_year >= 2005:
        return 0.4
    else:
        return 0.2


def score_strategy(strategy: dict, all_sharpes: list[float]) -> StrategyScore:
    """Compute all scores for a strategy."""
    rc = score_rule_clarity(strategy)
    da = score_data_accessibility(strategy)
    ss = score_sharpe(strategy, all_sharpes)
    rec = score_recency(strategy)

    composite = (
        WEIGHTS["rule_clarity"] * rc +
        WEIGHTS["data_access"] * da +
        WEIGHTS["claimed_sharpe"] * ss +
        WEIGHTS["recency"] * rec
    )

    notes = []
    if rc < 0.4:
        notes.append("vague rules")
    if da < 0.4:
        notes.append("hard-to-get data")
    if strategy.get("caveats") and "deteriorat" in strategy.get("caveats", "").lower():
        notes.append("declining performance noted")
        composite *= 0.85  # penalty

    return StrategyScore(
        strategy_id=strategy["id"],
        rule_clarity=round(rc, 2),
        data_access=round(da, 2),
        claimed_sharpe_score=round(ss, 2),
        recency_score=round(rec, 2),
        composite_score=round(composite, 3),
        notes="; ".join(notes) if notes else "",
    )


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def store_ranking(conn: sqlite3.Connection, score: StrategyScore, rank: int) -> None:
    conn.execute(
        """INSERT INTO strategy_rankings
           (strategy_id, rule_clarity, data_access, claimed_sharpe_score,
            recency_score, composite_score, rank, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            score.strategy_id, score.rule_clarity, score.data_access,
            score.claimed_sharpe_score, score.recency_score,
            score.composite_score, rank, score.notes,
        ),
    )


def store_duplicate(conn: sqlite3.Connection, kept_id: int, dup_id: int, reason: str) -> None:
    conn.execute(
        """INSERT INTO strategy_duplicates (kept_strategy_id, duplicate_strategy_id, similarity_reason)
           VALUES (?, ?, ?)""",
        (kept_id, dup_id, reason),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def catalog(dry_run: bool = False, db_path: str | None = None) -> list[dict]:
    """
    Deduplicate and rank all candidate strategies.
    """
    conn = init_db(db_path)

    # Get all candidate strategies (LLM-extracted only, id >= 5)
    rows = conn.execute(
        "SELECT * FROM strategies WHERE status = 'candidate' AND id >= 5 ORDER BY id"
    ).fetchall()
    strategies = [dict(r) for r in rows]

    if not strategies:
        log.info("No candidate strategies to catalog.")
        return []

    log.info(f"Cataloging {len(strategies)} candidate strategies...")

    log_agent_action(
        conn, "cataloger", "catalog_started",
        inputs={"count": len(strategies)},
    )

    # Step 1: Deduplication
    log.info("Step 1: Checking for duplicates...")
    duplicates = check_duplicates(conn, strategies)
    duplicate_ids = set()

    for kept_id, dup_id, reason in duplicates:
        log.info(f"  Duplicate found: [{dup_id}] is duplicate of [{kept_id}] — {reason}")
        duplicate_ids.add(dup_id)
        if not dry_run:
            store_duplicate(conn, kept_id, dup_id, reason)
            conn.execute(
                "UPDATE strategies SET status = 'duplicate', kill_reason = ? WHERE id = ?",
                (f"Duplicate of strategy {kept_id}: {reason}", dup_id),
            )

    # Filter out duplicates
    active = [s for s in strategies if s["id"] not in duplicate_ids]
    log.info(f"  {len(active)} unique strategies after dedup")

    # Step 2: Score and rank
    log.info("Step 2: Scoring strategies...")
    all_sharpes = [s["claimed_sharpe"] for s in active if s.get("claimed_sharpe")]

    scores = []
    for s in active:
        score = score_strategy(s, all_sharpes)
        scores.append((s, score))

    # Sort by composite score descending
    scores.sort(key=lambda x: x[1].composite_score, reverse=True)

    # Step 3: Store rankings
    results = []
    log.info("\nRanked strategies:")
    log.info(f"{'Rank':<5} {'Score':<7} {'Clarity':<8} {'Data':<6} {'Sharpe':<7} {'Recent':<7} {'Name'}")
    log.info("─" * 80)

    for rank, (strat, score) in enumerate(scores, 1):
        score_obj = score
        log.info(
            f"#{rank:<4} {score_obj.composite_score:<7.3f} "
            f"{score_obj.rule_clarity:<8.2f} {score_obj.data_access:<6.2f} "
            f"{score_obj.claimed_sharpe_score:<7.2f} {score_obj.recency_score:<7.2f} "
            f"{strat['name']}"
        )
        if score_obj.notes:
            log.info(f"      ⚠ {score_obj.notes}")

        if not dry_run:
            store_ranking(conn, score_obj, rank)

        results.append({
            "rank": rank,
            "strategy_id": strat["id"],
            "name": strat["name"],
            "composite_score": score_obj.composite_score,
            "rule_clarity": score_obj.rule_clarity,
            "data_access": score_obj.data_access,
            "sharpe_score": score_obj.claimed_sharpe_score,
            "recency_score": score_obj.recency_score,
            "notes": score_obj.notes,
            "entry_rule": strat["entry_rule"][:100],
        })

    if not dry_run:
        conn.commit()

    log_agent_action(
        conn, "cataloger", "catalog_completed",
        outputs={
            "ranked_count": len(results),
            "duplicates_found": len(duplicates),
            "top_strategy": results[0]["name"] if results else None,
        },
    )

    log.info(f"\nCatalog complete. {len(results)} strategies ranked, {len(duplicates)} duplicates found.")
    return results


def main():
    parser = argparse.ArgumentParser(description="Strategy Cataloger — deduplicate and rank")
    parser.add_argument("--dry-run", action="store_true", help="Score without storing")
    parser.add_argument("--db", type=str, default=None, help="Path to SQLite database")
    args = parser.parse_args()

    catalog(dry_run=args.dry_run, db_path=args.db)


if __name__ == "__main__":
    main()
