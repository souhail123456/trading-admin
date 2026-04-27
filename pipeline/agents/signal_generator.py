"""
Signal Generator Agent
----------------------
Runs daily. Fetches latest OHLCV data for sector ETFs, runs both strategies
(trend-following + price action), and outputs buy/sell signals to the DB.

Designed to run as a GitHub Actions cron or local cron job.

Usage:
    python3 -m pipeline.agents.signal_generator              # generate today's signals
    python3 -m pipeline.agents.signal_generator --dry-run    # print signals without storing
"""

import argparse
import json
import logging
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from pipeline.db import init_db, log_agent_action
from pipeline.agents.data_fetcher import fetch_ohlcv, SECTOR_ETFS, BENCHMARK
from pipeline.agents.price_action import detect_all_patterns

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

# Strategy IDs in the DB (trend-following = #18, price action = new)
# We'll use strategy_id=18 for trend signals, strategy_id=0 for price action
# (price action isn't in the strategies table yet — we'll handle that)
TREND_STRATEGY_ID = 18
PA_STRATEGY_ID = 99  # placeholder for price action signals


# ---------------------------------------------------------------------------
# Trend-following signal: which sectors are above SMA-200?
# ---------------------------------------------------------------------------

def trend_signals(data: dict[str, pd.DataFrame], sma_period: int = 200, top_n: int = 5) -> list[dict]:
    """
    Generate trend-following signals for today.
    Returns list of signal dicts: {symbol, side, signal_type, strength, state}
    """
    tickers = [t for t in data if t != BENCHMARK]
    signals = []

    candidates = []
    for t in tickers:
        df = data[t]
        if len(df) < sma_period + 10:
            continue

        sma = df["close"].rolling(sma_period).mean()
        latest = df.iloc[-1]
        latest_sma = sma.iloc[-1]

        above = latest["close"] > latest_sma
        strength = (latest["close"] - latest_sma) / latest_sma if latest_sma > 0 else 0

        state = {
            "close": round(float(latest["close"]), 2),
            "sma_200": round(float(latest_sma), 2),
            "above_sma": bool(above),
            "trend_strength": round(float(strength), 4),
            "date": str(df.index[-1].date()),
        }

        if above:
            candidates.append((t, strength, state))

    # Rank by trend strength, pick top_n
    candidates.sort(key=lambda x: x[1], reverse=True)

    for t, strength, state in candidates[:top_n]:
        signals.append({
            "strategy": "trend_following",
            "strategy_id": TREND_STRATEGY_ID,
            "symbol": t,
            "side": "long",
            "signal_type": "entry",
            "price_at_signal": state["close"],
            "full_state": state,
        })

    # Generate exit signals for tickers that dropped below SMA
    for t in tickers:
        df = data[t]
        if len(df) < sma_period + 10:
            continue
        sma = df["close"].rolling(sma_period).mean()
        latest = df.iloc[-1]

        if latest["close"] < sma.iloc[-1]:
            # Check if it was above SMA yesterday (new exit)
            if len(df) >= 2 and df.iloc[-2]["close"] > sma.iloc[-2]:
                signals.append({
                    "strategy": "trend_following",
                    "strategy_id": TREND_STRATEGY_ID,
                    "symbol": t,
                    "side": "long",
                    "signal_type": "exit",
                    "price_at_signal": round(float(latest["close"]), 2),
                    "full_state": {
                        "close": round(float(latest["close"]), 2),
                        "sma_200": round(float(sma.iloc[-1]), 2),
                        "reason": "dropped_below_sma",
                    },
                })

    return signals


# ---------------------------------------------------------------------------
# Price action signal: bullish/bearish patterns
# ---------------------------------------------------------------------------

def price_action_signals(data: dict[str, pd.DataFrame], min_score: int = 2) -> list[dict]:
    """
    Generate price action signals for today.
    Scans all tickers for bullish/bearish patterns.
    """
    tickers = [t for t in data if t != BENCHMARK]
    signals = []

    for t in tickers:
        df = data[t]
        if len(df) < 200:
            continue

        patterns = detect_all_patterns(df)
        if patterns.empty:
            continue

        latest = patterns.iloc[-1]

        state = {
            "date": str(patterns.index[-1].date()),
            "close": round(float(df.iloc[-1]["close"]), 2),
            "bull_score": int(latest["bull_score"]),
            "bear_score": int(latest["bear_score"]),
            "net_score": int(latest["net_score"]),
            "bullish_engulfing": int(latest["bullish_engulfing"]),
            "hammer": int(latest["hammer"]),
            "bullish_bos": int(latest["bullish_bos"]),
            "weekly_trend": int(latest["weekly_trend"]),
            "bearish_engulfing": int(latest["bearish_engulfing"]),
            "shooting_star": int(latest["shooting_star"]),
            "bearish_bos": int(latest["bearish_bos"]),
        }

        if latest["bull_signal"] > 0:
            signals.append({
                "strategy": "price_action",
                "strategy_id": PA_STRATEGY_ID,
                "symbol": t,
                "side": "long",
                "signal_type": "entry",
                "price_at_signal": state["close"],
                "full_state": state,
            })

        if latest["bear_signal"] > 0:
            signals.append({
                "strategy": "price_action",
                "strategy_id": PA_STRATEGY_ID,
                "symbol": t,
                "side": "long",
                "signal_type": "exit",
                "price_at_signal": state["close"],
                "full_state": state,
            })

    return signals


# ---------------------------------------------------------------------------
# Store signals
# ---------------------------------------------------------------------------

def store_signals(conn, signals: list[dict]) -> list[int]:
    """Insert signals into the DB. Returns list of signal IDs."""
    ids = []
    for s in signals:
        cursor = conn.execute(
            """INSERT INTO signals (strategy_id, signal_type, symbol, side, price_at_signal, full_state)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                s["strategy_id"],
                s["signal_type"],
                s["symbol"],
                s["side"],
                s["price_at_signal"],
                json.dumps(s["full_state"]),
            ),
        )
        ids.append(cursor.lastrowid)
    conn.commit()
    return ids


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def generate_signals(dry_run: bool = False, db_path: str | None = None) -> list[dict]:
    """
    Generate signals from both strategies.
    Fetches fresh data (last 250 trading days minimum for SMA-200).
    """
    conn = init_db(db_path)

    # Fetch recent data — need at least 250 days for SMA-200
    # Fetch from 2 years back to be safe
    start = (datetime.now() - timedelta(days=730)).strftime("%Y-%m-%d")
    tickers = list(SECTOR_ETFS.keys()) + [BENCHMARK]

    log.info(f"Fetching data for {len(tickers)} tickers from {start}...")
    data = fetch_ohlcv(tickers, start=start, cache=False)  # no cache — want fresh data
    log.info(f"Got {len(data)} tickers")

    # Generate signals from both strategies
    all_signals = []

    log.info("\n--- Trend-Following Signals ---")
    t_signals = trend_signals(data)
    all_signals.extend(t_signals)
    for s in t_signals:
        log.info(f"  [{s['signal_type'].upper()}] {s['symbol']} @ ${s['price_at_signal']} "
                 f"(strength: {s['full_state'].get('trend_strength', 'N/A')})")

    log.info("\n--- Price Action Signals ---")
    pa_sigs = price_action_signals(data)
    all_signals.extend(pa_sigs)
    for s in pa_sigs:
        log.info(f"  [{s['signal_type'].upper()}] {s['symbol']} @ ${s['price_at_signal']} "
                 f"(bull={s['full_state'].get('bull_score')}, bear={s['full_state'].get('bear_score')})")

    if not all_signals:
        log.info("\nNo signals generated today.")

    # Store
    if not dry_run and all_signals:
        ids = store_signals(conn, all_signals)
        log.info(f"\nStored {len(ids)} signals to DB")
        log_agent_action(
            conn, "signal_generator", "signals_generated",
            outputs={
                "count": len(all_signals),
                "trend_signals": len(t_signals),
                "pa_signals": len(pa_sigs),
                "signal_ids": ids,
            },
        )
    elif dry_run:
        log.info("\n[DRY RUN] Signals not stored.")

    # Summary
    print(f"\n{'='*70}")
    print(f"SIGNAL GENERATOR — {datetime.now().strftime('%Y-%m-%d')}")
    print(f"{'='*70}")

    entries = [s for s in all_signals if s["signal_type"] == "entry"]
    exits = [s for s in all_signals if s["signal_type"] == "exit"]

    if entries:
        print(f"\nENTRY SIGNALS ({len(entries)}):")
        for s in entries:
            tag = "TREND" if s["strategy"] == "trend_following" else "PA"
            print(f"  [{tag}] {s['symbol']:>5} LONG @ ${s['price_at_signal']}")

    if exits:
        print(f"\nEXIT SIGNALS ({len(exits)}):")
        for s in exits:
            tag = "TREND" if s["strategy"] == "trend_following" else "PA"
            reason = s["full_state"].get("reason", "pattern")
            print(f"  [{tag}] {s['symbol']:>5} EXIT @ ${s['price_at_signal']} ({reason})")

    if not all_signals:
        print("\n  No signals today. Both strategies say: hold or stay cash.")

    return all_signals


def main():
    parser = argparse.ArgumentParser(description="Signal Generator — daily signal scan")
    parser.add_argument("--dry-run", action="store_true", help="Print signals without storing")
    parser.add_argument("--db", type=str, default=None, help="Path to SQLite database")
    args = parser.parse_args()

    generate_signals(dry_run=args.dry_run, db_path=args.db)


if __name__ == "__main__":
    main()
