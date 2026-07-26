"""
Data Fetcher Agent
------------------
Pulls historical OHLCV data for each strategy's asset universe.
Caches locally in data/ to avoid repeat downloads.

Handles:
  - US sector ETFs (XLK, XLF, XLE, etc.)
  - US stocks (SPY benchmark + momentum universe)
  - Country ETFs (EWJ, EWG, FXI, etc.)
  - Currency pairs (via yfinance forex tickers)

Usage:
    python -m pipeline.agents.data_fetcher --strategy-id 5
    python -m pipeline.agents.data_fetcher --all
"""

import argparse
import glob
import json
import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

from pipeline.db import init_db, log_agent_action

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).parent.parent.parent / "data" / "ohlcv"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Ticker universes per strategy type
# ---------------------------------------------------------------------------

# US sector ETFs (SPDR Select Sector)
SECTOR_ETFS = {
    "XLB": "Materials",
    "XLC": "Communication Services",
    "XLE": "Energy",
    "XLF": "Financials",
    "XLI": "Industrials",
    "XLK": "Technology",
    "XLP": "Consumer Staples",
    "XLRE": "Real Estate",
    "XLU": "Utilities",
    "XLV": "Health Care",
    "XLY": "Consumer Discretionary",
}

# Country ETFs (iShares MSCI)
COUNTRY_ETFS = {
    "EWA": "Australia", "EWC": "Canada", "EWG": "Germany",
    "EWH": "Hong Kong", "EWI": "Italy", "EWJ": "Japan",
    "EWL": "Switzerland", "EWN": "Netherlands", "EWP": "Spain",
    "EWQ": "France", "EWS": "Singapore", "EWT": "Taiwan",
    "EWU": "UK", "EWW": "Mexico", "EWY": "South Korea",
    "EWZ": "Brazil", "FXI": "China", "EFA": "EAFE (developed ex-US)",
    "EEM": "Emerging Markets",
}

# Major currency pairs (yfinance format: XXXYYY=X)
CURRENCY_PAIRS = {
    "EURUSD=X": "EUR/USD", "GBPUSD=X": "GBP/USD", "USDJPY=X": "USD/JPY",
    "USDCHF=X": "USD/CHF", "AUDUSD=X": "AUD/USD", "USDCAD=X": "USD/CAD",
    "NZDUSD=X": "NZD/USD", "EURGBP=X": "EUR/GBP", "EURJPY=X": "EUR/JPY",
    "GBPJPY=X": "GBP/JPY",
}

# Benchmark
BENCHMARK = "SPY"


def get_tickers_for_strategy(strategy: dict) -> tuple[list[str], str]:
    """
    Determine which tickers to fetch based on strategy's asset universe.
    Returns (tickers, universe_type).
    """
    universe = strategy["asset_universe"].lower()
    name = strategy["name"].lower()

    if "sector" in name or "sector" in universe:
        tickers = list(SECTOR_ETFS.keys()) + [BENCHMARK]
        return tickers, "sector_etfs"

    if "country" in name or "country" in universe:
        tickers = list(COUNTRY_ETFS.keys()) + [BENCHMARK]
        return tickers, "country_etfs"

    if "currenc" in name or "fx" in universe or "currenc" in universe:
        tickers = list(CURRENCY_PAIRS.keys())
        return tickers, "currency_pairs"

    if any(kw in universe for kw in ["nyse", "nasdaq", "amex", "stock", "equit", "s&p"]):
        # For stock momentum, use sector ETFs as proxy for backtesting
        # (individual stock universe is too large for v1)
        tickers = list(SECTOR_ETFS.keys()) + [BENCHMARK]
        return tickers, "stock_proxy_etfs"

    # Default: sector ETFs + benchmark
    tickers = list(SECTOR_ETFS.keys()) + [BENCHMARK]
    return tickers, "unknown_default"


def _latest_cached(ticker: str, start: str) -> Path | None:
    """Find the most recent cached CSV for this ticker/start, regardless of end date."""
    prefix = f"{ticker.replace('=', '_')}_{start}_"
    candidates = sorted(CACHE_DIR.glob(f"{prefix}*.csv"))
    return candidates[-1] if candidates else None


def fetch_ohlcv(
    tickers: list[str],
    start: str = "2005-01-01",
    end: str | None = None,
    cache: bool = True,
    max_retries: int = 3,
    retry_backoff: float = 5.0,
    request_delay: float = 1.5,
) -> dict[str, pd.DataFrame]:
    """
    Fetch OHLCV data for a list of tickers.
    Caches each ticker as a CSV file.

    Yahoo Finance intermittently throttles/rejects rapid sequential requests
    (surfaces as a spurious "possibly delisted" error even for valid, liquid
    tickers). To make this resilient we retry with backoff, pace requests
    with a small delay, and fall back to the most recent cached copy of the
    ticker (even if its end-date doesn't match today) rather than silently
    dropping the ticker from the dataset.
    """
    if end is None:
        end = datetime.now().strftime("%Y-%m-%d")

    results = {}

    for ticker in tickers:
        cache_path = CACHE_DIR / f"{ticker.replace('=', '_')}_{start}_{end}.csv"

        if cache and cache_path.exists():
            log.info(f"  {ticker}: cached")
            results[ticker] = pd.read_csv(cache_path, index_col=0, parse_dates=True)
            continue

        df = None
        for attempt in range(1, max_retries + 1):
            log.info(f"  {ticker}: downloading... (attempt {attempt}/{max_retries})")
            try:
                candidate = yf.download(ticker, start=start, end=end, progress=False)
                if candidate is not None and not candidate.empty:
                    df = candidate
                    break
                log.warning(f"  {ticker}: no data returned (attempt {attempt}/{max_retries})")
            except Exception as e:
                log.error(f"  {ticker}: failed — {e} (attempt {attempt}/{max_retries})")

            if attempt < max_retries:
                time.sleep(retry_backoff * attempt)

        if df is not None:
            # Flatten multi-level columns if present
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            # Ensure standard columns
            df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
            df.columns = ["open", "high", "low", "close", "volume"]

            if cache:
                df.to_csv(cache_path)

            results[ticker] = df
        else:
            # All retries exhausted — fall back to the most recent cache
            # for this ticker (better than dropping it from the universe).
            fallback = _latest_cached(ticker, start) if cache else None
            if fallback:
                log.warning(f"  {ticker}: download failed after {max_retries} attempts — "
                            f"using stale cache {fallback.name}")
                results[ticker] = pd.read_csv(fallback, index_col=0, parse_dates=True)
            else:
                log.error(f"  {ticker}: download failed after {max_retries} attempts, no cache available — skipping")

        time.sleep(request_delay)

    return results


def fetch_for_strategy(
    strategy: dict,
    conn: sqlite3.Connection,
    start: str = "2005-01-01",
) -> dict[str, pd.DataFrame]:
    """
    Fetch all data needed for a strategy's backtest.
    """
    tickers, universe_type = get_tickers_for_strategy(strategy)

    log.info(f"Fetching {len(tickers)} tickers for [{strategy['id']}] {strategy['name']} ({universe_type})")

    log_agent_action(
        conn, "data_fetcher", "fetch_started",
        inputs={"strategy_id": strategy["id"], "tickers": tickers,
                "universe_type": universe_type, "start": start},
        strategy_id=strategy["id"],
    )

    data = fetch_ohlcv(tickers, start=start)

    log_agent_action(
        conn, "data_fetcher", "fetch_completed",
        inputs={"strategy_id": strategy["id"]},
        outputs={
            "tickers_fetched": len(data),
            "tickers_failed": len(tickers) - len(data),
            "date_range": {
                t: f"{df.index[0].strftime('%Y-%m-%d')} to {df.index[-1].strftime('%Y-%m-%d')}"
                for t, df in list(data.items())[:3]
            },
        },
        strategy_id=strategy["id"],
    )

    return data


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def fetch_all(
    strategy_id: int | None = None,
    db_path: str | None = None,
) -> None:
    conn = init_db(db_path)

    if strategy_id:
        rows = conn.execute("SELECT * FROM strategies WHERE id = ?", (strategy_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM strategies WHERE status = 'candidate' AND id >= 5 ORDER BY id"
        ).fetchall()

    strategies = [dict(r) for r in rows]

    if not strategies:
        log.info("No strategies to fetch data for.")
        return

    for strat in strategies:
        data = fetch_for_strategy(strat, conn)
        log.info(f"  Got {len(data)} tickers, "
                 f"date range example: {list(data.values())[0].index[0].strftime('%Y-%m-%d')} "
                 f"to {list(data.values())[0].index[-1].strftime('%Y-%m-%d')}" if data else "  No data")
        print()


def main():
    parser = argparse.ArgumentParser(description="Data Fetcher — pull OHLCV data")
    parser.add_argument("--strategy-id", type=int, default=None)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--db", type=str, default=None)
    args = parser.parse_args()

    sid = args.strategy_id if not args.all else None
    fetch_all(strategy_id=sid, db_path=args.db)


if __name__ == "__main__":
    main()
