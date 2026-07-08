"""
FX Signal Generator
-------------------
Daily scan of 10 major currency pairs for trend-following + price action signals.
Outputs to DB signals table, ready for risk manager and execution.

Usage:
    python3 -m pipeline.agents.fx_signal_generator              # generate signals
    python3 -m pipeline.agents.fx_signal_generator --dry-run    # print only
"""

import argparse
import json
import logging
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from pipeline.db import init_db, log_agent_action, get_strategy_params
from pipeline.agents.data_fetcher import fetch_ohlcv, CURRENCY_PAIRS
from pipeline.agents.price_action import detect_all_patterns

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

# Strategy IDs
FX_TREND_STRATEGY_ID = 100
FX_PA_STRATEGY_ID = 101

# ---------------------------------------------------------------------------
# Carry trade overlay — central bank policy rates (updated manually)
# ---------------------------------------------------------------------------
CENTRAL_BANK_RATES = {
    "USD": 4.50, "EUR": 2.65, "GBP": 4.50, "JPY": 0.50,
    "CHF": 0.25, "AUD": 4.10, "CAD": 2.75, "NZD": 3.50,
}


def calculate_carry(pair: str) -> float:
    """Return annualized carry (%) for going long the pair.
    Long = buy base (first 3 chars), sell quote (last 3 chars).
    E.g. AUDUSD long: AUD rate - USD rate = 4.10 - 4.50 = -0.40
    """
    base = pair[:3]
    quote = pair[3:6]
    return CENTRAL_BANK_RATES.get(base, 0.0) - CENTRAL_BANK_RATES.get(quote, 0.0)

# yfinance ticker to clean symbol mapping
def _clean_symbol(ticker: str) -> str:
    """EURUSD=X -> EURUSD"""
    return ticker.replace("=X", "")


# ---------------------------------------------------------------------------
# FX Trend-following signals
# ---------------------------------------------------------------------------

def fx_trend_signals(data: dict[str, pd.DataFrame], params: dict | None = None) -> list[dict]:
    """
    Scan FX pairs: which are above SMA? Rank by trend strength.
    Parameters read from DB strategy params.
    """
    params = params or {}
    sma_period = params.get("sma_period", 200)
    top_n = params.get("top_n", 3)
    signals = []
    candidates = []

    for ticker, df in data.items():
        if len(df) < sma_period + 10:
            continue

        sma = df["close"].rolling(sma_period).mean()
        latest = df.iloc[-1]
        latest_sma = sma.iloc[-1]
        above = latest["close"] > latest_sma
        strength = (latest["close"] - latest_sma) / latest_sma if latest_sma > 0 else 0

        pair = _clean_symbol(ticker)
        carry = calculate_carry(pair)
        carry_normalized = carry / 10.0
        composite = float(strength) * 0.7 + carry_normalized * 0.3

        state = {
            "close": round(float(latest["close"]), 5),
            "sma_200": round(float(latest_sma), 5),
            "above_sma": bool(above),
            "trend_strength": round(float(strength), 4),
            "carry_pct": round(carry, 2),
            "carry_normalized": round(carry_normalized, 4),
            "composite_score": round(composite, 4),
            "date": str(df.index[-1].date()),
            "pair": pair,
        }

        if above:
            candidates.append((ticker, composite, state))
        else:
            # Exit signal: price is below SMA-200
            # Fire on any day below (not just crossunder day) to avoid missing exits
            signals.append({
                "strategy": "fx_trend",
                "strategy_id": FX_TREND_STRATEGY_ID,
                "symbol": _clean_symbol(ticker),
                "side": "long",
                "signal_type": "exit",
                "price_at_signal": round(float(latest["close"]), 5),
                "full_state": {**state, "reason": "below_sma"},
            })

    # Top N by composite score (trend strength * 0.7 + carry * 0.3)
    candidates.sort(key=lambda x: x[1], reverse=True)
    for ticker, composite, state in candidates[:top_n]:
        log.info(f"  CARRY: {state['pair']} carry={state['carry_pct']:+.2f}% "
                 f"trend={state['trend_strength']:.4f} composite={state['composite_score']:.4f}")
        signals.append({
            "strategy": "fx_trend",
            "strategy_id": FX_TREND_STRATEGY_ID,
            "symbol": _clean_symbol(ticker),
            "side": "long",
            "signal_type": "entry",
            "price_at_signal": state["close"],
            "full_state": state,
        })

    return signals


# ---------------------------------------------------------------------------
# FX Price action signals
# ---------------------------------------------------------------------------

def fx_pa_signals(data: dict[str, pd.DataFrame], params: dict | None = None) -> list[dict]:
    """Scan FX pairs for bullish/bearish candlestick + structure patterns.
    Parameters read from DB strategy params."""
    params = params or {}
    min_score = params.get("min_bull_score", 2)
    signals = []

    for ticker, df in data.items():
        if len(df) < 200:
            continue

        patterns = detect_all_patterns(df)
        if patterns.empty:
            continue

        latest = patterns.iloc[-1]
        state = {
            "date": str(patterns.index[-1].date()),
            "close": round(float(df.iloc[-1]["close"]), 5),
            "pair": _clean_symbol(ticker),
            "bull_score": int(latest["bull_score"]),
            "bear_score": int(latest["bear_score"]),
            "net_score": int(latest["net_score"]),
            "bullish_engulfing": int(latest["bullish_engulfing"]),
            "hammer": int(latest["hammer"]),
            "bullish_bos": int(latest["bullish_bos"]),
            "weekly_trend": int(latest["weekly_trend"]),
        }

        if latest["bull_signal"] > 0:
            signals.append({
                "strategy": "fx_price_action",
                "strategy_id": FX_PA_STRATEGY_ID,
                "symbol": _clean_symbol(ticker),
                "side": "long",
                "signal_type": "entry",
                "price_at_signal": state["close"],
                "full_state": state,
            })

        if latest["bear_signal"] > 0:
            signals.append({
                "strategy": "fx_price_action",
                "strategy_id": FX_PA_STRATEGY_ID,
                "symbol": _clean_symbol(ticker),
                "side": "long",
                "signal_type": "exit",
                "price_at_signal": state["close"],
                "full_state": state,
            })

    return signals


# ---------------------------------------------------------------------------
# Store & run
# ---------------------------------------------------------------------------

def store_signals(conn, signals: list[dict]) -> list[int]:
    ids = []
    for s in signals:
        cursor = conn.execute(
            """INSERT INTO signals (strategy_id, signal_type, symbol, side, price_at_signal, full_state)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (s["strategy_id"], s["signal_type"], s["symbol"], s["side"],
             s["price_at_signal"], json.dumps(s["full_state"])),
        )
        ids.append(cursor.lastrowid)
    conn.commit()
    return ids


def generate_fx_signals(dry_run: bool = False, db_path: str | None = None) -> list[dict]:
    conn = init_db(db_path)

    # Fetch 2 years of FX data (fresh, no cache)
    start = (datetime.now() - timedelta(days=730)).strftime("%Y-%m-%d")
    tickers = list(CURRENCY_PAIRS.keys())

    log.info(f"Fetching {len(tickers)} FX pairs from {start}...")
    data = fetch_ohlcv(tickers, start=start, cache=False)
    log.info(f"Got {len(data)} pairs")

    all_signals = []

    # Load strategy parameters from DB
    trend_params = get_strategy_params(conn, FX_TREND_STRATEGY_ID)
    pa_params = get_strategy_params(conn, FX_PA_STRATEGY_ID)
    log.info(f"Trend params: {trend_params}")
    log.info(f"PA params: {pa_params}")

    log.info("\n--- FX Trend Signals ---")
    t_sigs = fx_trend_signals(data, params=trend_params)
    all_signals.extend(t_sigs)
    for s in t_sigs:
        log.info(f"  [{s['signal_type'].upper()}] {s['symbol']} @ {s['price_at_signal']} "
                 f"(strength: {s['full_state'].get('trend_strength', 'N/A')})")

    log.info("\n--- FX Price Action Signals ---")
    pa_sigs = fx_pa_signals(data, params=pa_params)
    all_signals.extend(pa_sigs)
    for s in pa_sigs:
        log.info(f"  [{s['signal_type'].upper()}] {s['symbol']} @ {s['price_at_signal']} "
                 f"(bull={s['full_state'].get('bull_score')}, bear={s['full_state'].get('bear_score')})")

    if not all_signals:
        log.info("\nNo FX signals today.")

    if not dry_run and all_signals:
        ids = store_signals(conn, all_signals)
        log.info(f"\nStored {len(ids)} FX signals")
        log_agent_action(conn, "fx_signal_generator", "signals_generated",
                         outputs={"count": len(all_signals), "trend": len(t_sigs), "pa": len(pa_sigs)})
    elif dry_run:
        log.info("\n[DRY RUN] Signals not stored.")

    # Summary
    print(f"\n{'='*70}")
    print(f"FX SIGNAL GENERATOR — {datetime.now().strftime('%Y-%m-%d')}")
    print(f"{'='*70}")

    entries = [s for s in all_signals if s["signal_type"] == "entry"]
    exits = [s for s in all_signals if s["signal_type"] == "exit"]

    if entries:
        print(f"\nENTRY SIGNALS ({len(entries)}):")
        for s in entries:
            tag = "TREND" if "trend" in s["strategy"] else "PA"
            print(f"  [{tag}] {s['symbol']:>8} LONG @ {s['price_at_signal']}")

    if exits:
        print(f"\nEXIT SIGNALS ({len(exits)}):")
        for s in exits:
            tag = "TREND" if "trend" in s["strategy"] else "PA"
            print(f"  [{tag}] {s['symbol']:>8} EXIT @ {s['price_at_signal']}")

    if not all_signals:
        print("\n  No FX signals today.")

    return all_signals


def main():
    parser = argparse.ArgumentParser(description="FX Signal Generator")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", type=str, default=None)
    args = parser.parse_args()
    generate_fx_signals(dry_run=args.dry_run, db_path=args.db)


if __name__ == "__main__":
    main()
