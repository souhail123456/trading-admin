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

from pipeline.db import init_db, log_agent_action, get_strategy_params, KILLED_STRATEGIES
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

# Entry-confirmation filter defaults for fx_trend_signals(), set from the
# backtest gate (fx_backtester.compare_entry_filters, full sample 2006-2026):
#   baseline SMA-200:  Sharpe 2.279, MaxDD -3.11%, 720 trades
#   + MACD:            Sharpe 2.960, MaxDD -3.15%, 536 trades  <-- best
#   + RSI:             Sharpe 1.685, MaxDD -6.53%, 701 trades  (hurts)
#   + MACD + RSI:      Sharpe 2.355, MaxDD -4.19%, 476 trades  (RSI drags MACD down)
# => MACD ON (clear Sharpe lift, trade count still healthy); RSI OFF (degrades
#    Sharpe standalone and in combination). Overridable per-run via strategy params.
MACD_FILTER_DEFAULT = True
RSI_FILTER_DEFAULT = False

# Belt-and-suspenders: strategies killed in code, checked BEFORE any DB lookup.
# This is a secondary check — KILLED_STRATEGIES in db.py is the primary source.
KILLED_STRATEGY_IDS: set[int] = set(KILLED_STRATEGIES.keys())

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
# Entry-confirmation indicators (vectorized) — MACD histogram + RSI
# ---------------------------------------------------------------------------

def macd_histogram(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.Series:
    """MACD histogram = (EMA_fast - EMA_slow) - EMA_signal(MACD).
    Positive = bullish momentum, negative = bearish momentum."""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    signal_line = macd.ewm(span=signal, adjust=False).mean()
    return macd - signal_line


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI-14 using EMA smoothing (alpha=1/period)."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# ---------------------------------------------------------------------------
# FX Trend-following signals
# ---------------------------------------------------------------------------

def fx_trend_signals(
    data: dict[str, pd.DataFrame],
    params: dict | None = None,
    open_positions: list[dict] | None = None,
) -> list[dict]:
    """
    Scan FX pairs for trend-following signals (both long and short).

    Entry logic:
      - Price ABOVE SMA-200 and pair NOT held → long entry candidate (ranked by composite score)
      - Price BELOW SMA-200 and pair NOT held → short entry candidate (ranked by abs composite score)

    Exit logic (position-aware — only fires for pairs we actually hold):
      - Holding LONG and price dropped BELOW SMA-200 → exit long signal
      - Holding SHORT and price rose ABOVE SMA-200 → exit short signal

    Parameters read from DB strategy params.
    open_positions: list of dicts with at least {"symbol": str, "side": "long"|"short"}
    """
    params = params or {}
    sma_period = params.get("sma_period", 200)
    top_n = params.get("top_n", 3)
    # Entry-confirmation filters (defaults set from backtest — see fx_backtester
    # compare_entry_filters). Applied to ENTRY candidates only, never to exits.
    macd_filter = params.get("macd_filter", MACD_FILTER_DEFAULT)
    rsi_filter = params.get("rsi_filter", RSI_FILTER_DEFAULT)
    rsi_overbought = params.get("rsi_overbought", 70)
    rsi_oversold = params.get("rsi_oversold", 30)
    signals = []

    # Build lookup of currently held positions: symbol -> side
    held: dict[str, str] = {}
    for pos in (open_positions or []):
        held[pos["symbol"]] = pos.get("side", "long")

    long_candidates = []   # pairs above SMA not currently held
    short_candidates = []  # pairs below SMA not currently held

    for ticker, df in data.items():
        if len(df) < sma_period + 10:
            continue

        sma = df["close"].rolling(sma_period).mean()
        latest = df.iloc[-1]
        latest_sma = sma.iloc[-1]
        above = latest["close"] > latest_sma
        strength = (latest["close"] - latest_sma) / latest_sma if latest_sma > 0 else 0

        # Entry-confirmation indicators (latest bar)
        latest_hist = float(macd_histogram(df["close"]).iloc[-1])
        latest_rsi = float(rsi(df["close"]).iloc[-1])

        pair = _clean_symbol(ticker)
        carry = calculate_carry(pair)
        carry_normalized = carry / 10.0

        # Long composite: positive strength (above SMA) + carry
        long_composite = float(strength) * 0.7 + carry_normalized * 0.3
        # Short composite: abs(strength) (below SMA) minus carry (pay carry when short base)
        short_composite = abs(float(strength)) * 0.7 - carry_normalized * 0.3

        state = {
            "close": round(float(latest["close"]), 5),
            "sma_200": round(float(latest_sma), 5),
            "above_sma": bool(above),
            "trend_strength": round(float(strength), 4),
            "carry_pct": round(carry, 2),
            "carry_normalized": round(carry_normalized, 4),
            "composite_score": round(long_composite, 4),
            "macd_histogram": round(latest_hist, 6),
            "rsi_14": round(latest_rsi, 2),
            "date": str(df.index[-1].date()),
            "pair": pair,
        }

        current_side = held.get(pair)

        if above:
            # --- Price is ABOVE SMA-200 ---
            if current_side == "short":
                # Holding a short and price rose back above SMA → exit short
                log.info(f"  EXIT SHORT: {pair} — price {state['close']} rose above SMA {state['sma_200']}")
                signals.append({
                    "strategy": "fx_trend",
                    "strategy_id": FX_TREND_STRATEGY_ID,
                    "symbol": pair,
                    "side": "short",
                    "signal_type": "exit",
                    "price_at_signal": state["close"],
                    "full_state": {**state, "reason": "above_sma_short_exit"},
                })
            elif current_side is None:
                # Not holding this pair → long entry candidate, subject to filters.
                # MACD: require momentum to agree (histogram > 0).
                # RSI: skip buying into overbought exhaustion.
                if macd_filter and not (latest_hist > 0):
                    log.info(f"  SKIP LONG {pair}: MACD hist {latest_hist:.6f} <= 0")
                elif rsi_filter and latest_rsi >= rsi_overbought:
                    log.info(f"  SKIP LONG {pair}: RSI {latest_rsi:.1f} >= {rsi_overbought} (overbought)")
                else:
                    long_candidates.append((ticker, long_composite, state))
            # If already holding long: stay in trade, no signal

        else:
            # --- Price is BELOW SMA-200 ---
            if current_side == "long":
                # Holding a long and price dropped below SMA → exit long
                log.info(f"  EXIT LONG: {pair} — price {state['close']} dropped below SMA {state['sma_200']}")
                signals.append({
                    "strategy": "fx_trend",
                    "strategy_id": FX_TREND_STRATEGY_ID,
                    "symbol": pair,
                    "side": "long",
                    "signal_type": "exit",
                    "price_at_signal": state["close"],
                    "full_state": {**state, "reason": "below_sma_long_exit"},
                })
            elif current_side is None:
                # Not holding this pair → short entry candidate, subject to filters.
                # MACD: require momentum to agree (histogram < 0).
                # RSI: skip selling into oversold exhaustion.
                if macd_filter and not (latest_hist < 0):
                    log.info(f"  SKIP SHORT {pair}: MACD hist {latest_hist:.6f} >= 0")
                elif rsi_filter and latest_rsi <= rsi_oversold:
                    log.info(f"  SKIP SHORT {pair}: RSI {latest_rsi:.1f} <= {rsi_oversold} (oversold)")
                else:
                    short_state = {**state, "composite_score": round(short_composite, 4)}
                    short_candidates.append((ticker, short_composite, short_state))
            # If already holding short: stay in trade, no signal

    # Top N long entries by composite score (trend_strength * 0.7 + carry * 0.3)
    long_candidates.sort(key=lambda x: x[1], reverse=True)
    for ticker, composite, state in long_candidates[:top_n]:
        log.info(f"  LONG ENTRY: {state['pair']} carry={state['carry_pct']:+.2f}% "
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

    # Top N short entries by short composite score (abs(strength) * 0.7 - carry * 0.3)
    short_candidates.sort(key=lambda x: x[1], reverse=True)
    for ticker, composite, state in short_candidates[:top_n]:
        log.info(f"  SHORT ENTRY: {state['pair']} carry={state['carry_pct']:+.2f}% "
                 f"trend={state['trend_strength']:.4f} short_composite={state['composite_score']:.4f}")
        signals.append({
            "strategy": "fx_trend",
            "strategy_id": FX_TREND_STRATEGY_ID,
            "symbol": _clean_symbol(ticker),
            "side": "short",
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

        patterns = detect_all_patterns(df, min_score=min_score)
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

    # Load open trend positions so exit/entry logic is position-aware
    open_trend_rows = conn.execute(
        "SELECT symbol, side FROM paper_trades WHERE status = 'open' AND strategy_id = ?",
        (FX_TREND_STRATEGY_ID,),
    ).fetchall()
    open_trend_positions = [dict(r) for r in open_trend_rows]
    open_pos_desc = [p["symbol"] + " " + p["side"] for p in open_trend_positions]
    log.info(f"Open trend positions: {open_pos_desc}")

    log.info("\n--- FX Trend Signals ---")
    t_sigs = fx_trend_signals(data, params=trend_params, open_positions=open_trend_positions)
    all_signals.extend(t_sigs)
    for s in t_sigs:
        log.info(f"  [{s['signal_type'].upper()}] {s['symbol']} @ {s['price_at_signal']} "
                 f"(strength: {s['full_state'].get('trend_strength', 'N/A')})")

    # Check if PA strategy is killed — code-level check first (survives DB resets),
    # then DB check as fallback for strategies killed at runtime
    pa_killed = FX_PA_STRATEGY_ID in KILLED_STRATEGY_IDS
    if not pa_killed:
        pa_status = conn.execute(
            "SELECT status FROM strategies WHERE id = ?", (FX_PA_STRATEGY_ID,)
        ).fetchone()
        pa_killed = pa_status and pa_status["status"] == "killed"

    if pa_killed:
        kill_reason = KILLED_STRATEGIES.get(FX_PA_STRATEGY_ID, "killed in DB")
        log.info(f"\n--- FX Price Action Signals --- SKIPPED (strategy killed: {kill_reason})")
        pa_sigs = []
    else:
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
            print(f"  [{tag}] {s['symbol']:>8} {s['side'].upper()} @ {s['price_at_signal']}")

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
