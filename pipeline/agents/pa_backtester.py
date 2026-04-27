"""
Price Action Backtester
-----------------------
Compares three strategy variants on sector ETF data:
  1. Trend-following alone (SMA-200 filter, top 5 by trend strength)
  2. Price action alone (enter on bullish patterns, exit on bearish)
  3. Merged: trend filter picks WHAT + price action times WHEN

All use the same 20-year dataset with 70/30 in-sample/out-of-sample split.

Usage:
    python -m pipeline.agents.pa_backtester
"""

import json
import logging
import sqlite3

import numpy as np
import pandas as pd

from pipeline.db import init_db, log_agent_action
from pipeline.agents.data_fetcher import fetch_ohlcv, SECTOR_ETFS, BENCHMARK
from pipeline.agents.price_action import detect_all_patterns
from pipeline.agents.backtester import compute_metrics, trend_following

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Strategy 1: Trend-following alone (reuse existing implementation)
# ---------------------------------------------------------------------------

def run_trend_only(data: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Run the existing trend-following strategy as baseline."""
    results, holdings, trades = trend_following(
        data, sma_period=200, top_n=5, benchmark_ticker=BENCHMARK
    )
    return results


# ---------------------------------------------------------------------------
# Strategy 2: Price action standalone
# ---------------------------------------------------------------------------

def run_price_action_only(
    data: dict[str, pd.DataFrame],
    min_score: int = 2,
    max_positions: int = 5,
    hold_days: int = 20,  # ~1 month holding period
    stop_loss: float = -0.05,  # 5% stop loss
) -> pd.DataFrame:
    """
    Pure price action strategy:
    - Scan all tickers daily for bullish patterns (score >= min_score)
    - Enter equal-weight long positions (max max_positions)
    - Exit after hold_days OR if bearish signal OR stop loss hit
    - Rebalance monthly to stay aligned with monthly benchmark comparison
    """
    tickers = [t for t in data if t != BENCHMARK]

    # Detect patterns for all tickers
    patterns = {}
    for t in tickers:
        if len(data[t]) < 200:
            continue
        patterns[t] = detect_all_patterns(data[t])

    if not patterns:
        return pd.DataFrame()

    # Build daily portfolio
    # Get common date index
    all_dates = sorted(set().union(*(p.index for p in patterns.values())))
    all_dates = pd.DatetimeIndex(all_dates)

    # Track positions: {ticker: {"entry_date": date, "entry_price": float}}
    positions = {}
    daily_returns = []

    for i, date in enumerate(all_dates):
        if i == 0:
            continue

        prev_date = all_dates[i - 1]

        # Calculate return from current positions
        pos_returns = []
        to_exit = []

        for ticker, pos in positions.items():
            if ticker not in data or date not in data[ticker].index:
                continue
            if prev_date not in data[ticker].index:
                continue

            current_price = data[ticker].loc[date, "close"]
            prev_price = data[ticker].loc[prev_date, "close"]
            day_ret = (current_price - prev_price) / prev_price
            pos_returns.append(day_ret)

            # Check exit conditions
            total_ret = (current_price - pos["entry_price"]) / pos["entry_price"]
            days_held = (date - pos["entry_date"]).days

            # Exit on: stop loss, hold period, or bearish signal
            exit_signal = False
            if ticker in patterns and date in patterns[ticker].index:
                exit_signal = patterns[ticker].loc[date, "bear_signal"] > 0

            if total_ret <= stop_loss or days_held >= hold_days or exit_signal:
                to_exit.append(ticker)

        # Portfolio return (equal weight across active positions)
        if pos_returns:
            port_ret = np.mean(pos_returns)
        else:
            port_ret = 0.0

        # Exit positions
        for t in to_exit:
            del positions[t]

        # Enter new positions if slots available
        slots = max_positions - len(positions)
        if slots > 0:
            candidates = []
            for t in tickers:
                if t in positions:
                    continue
                if t not in patterns:
                    continue
                if date not in patterns[t].index:
                    continue
                score = patterns[t].loc[date, "bull_score"]
                if score >= min_score:
                    candidates.append((t, score))

            # Pick top by score
            candidates.sort(key=lambda x: x[1], reverse=True)
            for t, _ in candidates[:slots]:
                if date in data[t].index:
                    positions[t] = {
                        "entry_date": date,
                        "entry_price": data[t].loc[date, "close"],
                    }

        # Benchmark return
        bench_ret = 0.0
        if BENCHMARK in data and date in data[BENCHMARK].index and prev_date in data[BENCHMARK].index:
            bp = data[BENCHMARK].loc[prev_date, "close"]
            bc = data[BENCHMARK].loc[date, "close"]
            bench_ret = (bc - bp) / bp

        daily_returns.append({
            "date": date,
            "return": port_ret,
            "benchmark": bench_ret,
            "n_positions": len(positions),
        })

    if not daily_returns:
        return pd.DataFrame()

    results = pd.DataFrame(daily_returns).set_index("date")

    # Resample to monthly for consistent comparison with other strategies
    monthly = results.resample("ME").agg({
        "return": lambda x: (1 + x).prod() - 1,
        "benchmark": lambda x: (1 + x).prod() - 1,
        "n_positions": "mean",
    })
    monthly["cumulative"] = (1 + monthly["return"]).cumprod()
    monthly["bench_cumulative"] = (1 + monthly["benchmark"]).cumprod()

    return monthly


# ---------------------------------------------------------------------------
# Strategy 3: Merged (trend filter + price action entry)
# ---------------------------------------------------------------------------

def run_merged(
    data: dict[str, pd.DataFrame],
    sma_period: int = 200,
    top_n: int = 5,
    min_pa_score: int = 1,  # lower threshold since trend filter already provides confirmation
    hold_days: int = 20,
    stop_loss: float = -0.05,
) -> pd.DataFrame:
    """
    Merged strategy:
    - Trend filter: only consider tickers above SMA-200 (same as trend-following)
    - Rank by trend strength, limit to top_n candidates
    - Price action entry: only enter when bullish pattern detected (score >= min_pa_score)
    - Exit: bearish pattern, stop loss, or ticker drops below SMA
    """
    tickers = [t for t in data if t != BENCHMARK]

    # Compute trend signals
    trend_signals = {}
    for t in tickers:
        df = data[t].copy()
        if len(df) < sma_period + 50:
            continue
        df["sma"] = df["close"].rolling(sma_period).mean()
        df["above_sma"] = df["close"] > df["sma"]
        df["trend_strength"] = (df["close"] - df["sma"]) / df["sma"]
        trend_signals[t] = df

    # Detect price action patterns
    patterns = {}
    for t in tickers:
        if t not in trend_signals:
            continue
        patterns[t] = detect_all_patterns(data[t])

    if not patterns:
        return pd.DataFrame()

    all_dates = sorted(set().union(*(data[t].index for t in trend_signals)))
    all_dates = pd.DatetimeIndex(all_dates)

    positions = {}
    daily_returns = []

    for i, date in enumerate(all_dates):
        if i == 0:
            continue

        prev_date = all_dates[i - 1]

        # Calculate return from current positions
        pos_returns = []
        to_exit = []

        for ticker, pos in positions.items():
            if date not in data[ticker].index or prev_date not in data[ticker].index:
                continue

            current_price = data[ticker].loc[date, "close"]
            prev_price = data[ticker].loc[prev_date, "close"]
            day_ret = (current_price - prev_price) / prev_price
            pos_returns.append(day_ret)

            total_ret = (current_price - pos["entry_price"]) / pos["entry_price"]
            days_held = (date - pos["entry_date"]).days

            # Exit conditions for merged:
            # 1. Ticker drops below SMA (trend filter says get out)
            below_sma = False
            if ticker in trend_signals and date in trend_signals[ticker].index:
                below_sma = not trend_signals[ticker].loc[date, "above_sma"]

            # 2. Bearish price action signal
            bear_pa = False
            if ticker in patterns and date in patterns[ticker].index:
                bear_pa = patterns[ticker].loc[date, "bear_signal"] > 0

            # 3. Stop loss
            # 4. Max hold (but extend if still in trend + no bearish signal)
            force_exit = total_ret <= stop_loss or below_sma or bear_pa
            time_exit = days_held >= hold_days and not (
                ticker in trend_signals
                and date in trend_signals[ticker].index
                and trend_signals[ticker].loc[date, "above_sma"]
            )

            if force_exit or time_exit:
                to_exit.append(ticker)

        if pos_returns:
            port_ret = np.mean(pos_returns)
        else:
            port_ret = 0.0

        for t in to_exit:
            del positions[t]

        # Enter: ticker must be (a) above SMA, (b) in top_n trend strength, (c) bullish PA signal
        slots = top_n - len(positions)
        if slots > 0:
            # Get trend-filtered candidates
            trend_candidates = []
            for t in tickers:
                if t in positions or t not in trend_signals:
                    continue
                if date not in trend_signals[t].index:
                    continue
                row = trend_signals[t].loc[date]
                if row["above_sma"]:
                    trend_candidates.append((t, row["trend_strength"]))

            # Rank by trend strength
            trend_candidates.sort(key=lambda x: x[1], reverse=True)
            top_trend = [c[0] for c in trend_candidates[:top_n]]

            # Among top trend, only enter if PA gives bullish signal
            for t in top_trend:
                if len(positions) >= top_n:
                    break
                if t in positions:
                    continue
                if t not in patterns or date not in patterns[t].index:
                    continue
                if patterns[t].loc[date, "bull_score"] >= min_pa_score:
                    if date in data[t].index:
                        positions[t] = {
                            "entry_date": date,
                            "entry_price": data[t].loc[date, "close"],
                        }

        # Benchmark
        bench_ret = 0.0
        if BENCHMARK in data and date in data[BENCHMARK].index and prev_date in data[BENCHMARK].index:
            bp = data[BENCHMARK].loc[prev_date, "close"]
            bc = data[BENCHMARK].loc[date, "close"]
            bench_ret = (bc - bp) / bp

        daily_returns.append({
            "date": date,
            "return": port_ret,
            "benchmark": bench_ret,
            "n_positions": len(positions),
        })

    if not daily_returns:
        return pd.DataFrame()

    results = pd.DataFrame(daily_returns).set_index("date")

    monthly = results.resample("ME").agg({
        "return": lambda x: (1 + x).prod() - 1,
        "benchmark": lambda x: (1 + x).prod() - 1,
        "n_positions": "mean",
    })
    monthly["cumulative"] = (1 + monthly["return"]).cumprod()
    monthly["bench_cumulative"] = (1 + monthly["benchmark"]).cumprod()

    return monthly


# ---------------------------------------------------------------------------
# Compare all three
# ---------------------------------------------------------------------------

def compare_all(db_path: str | None = None) -> dict:
    """
    Run all 3 variants and produce comparison metrics.
    Uses 70/30 in-sample/out-of-sample split.
    """
    conn = init_db(db_path)

    log.info("Loading sector ETF data...")
    tickers = list(SECTOR_ETFS.keys()) + [BENCHMARK]
    data = fetch_ohlcv(tickers, start="2005-01-01")
    log.info(f"Loaded {len(data)} tickers")

    results = {}

    # --- Variant 1: Trend-following alone ---
    log.info("\n=== VARIANT 1: Trend-Following Alone ===")
    trend_results = run_trend_only(data)
    if not trend_results.empty:
        split = int(len(trend_results) * 0.70)
        oos = trend_results.iloc[split:].copy()
        oos["cumulative"] = (1 + oos["return"]).cumprod()
        oos["bench_cumulative"] = (1 + oos["benchmark"]).cumprod()
        ism = trend_results.iloc[:split].copy()
        ism["cumulative"] = (1 + ism["return"]).cumprod()
        ism["bench_cumulative"] = (1 + ism["benchmark"]).cumprod()
        results["trend_only"] = {
            "in_sample": compute_metrics(ism),
            "out_of_sample": compute_metrics(oos),
            "full": compute_metrics(trend_results),
            "total_months": len(trend_results),
        }
        log.info(f"  IS:  {results['trend_only']['in_sample']}")
        log.info(f"  OOS: {results['trend_only']['out_of_sample']}")

    # --- Variant 2: Price Action Alone ---
    log.info("\n=== VARIANT 2: Price Action Alone ===")
    pa_results = run_price_action_only(data)
    if not pa_results.empty:
        split = int(len(pa_results) * 0.70)
        oos = pa_results.iloc[split:].copy()
        oos["cumulative"] = (1 + oos["return"]).cumprod()
        oos["bench_cumulative"] = (1 + oos["benchmark"]).cumprod()
        ism = pa_results.iloc[:split].copy()
        ism["cumulative"] = (1 + ism["return"]).cumprod()
        ism["bench_cumulative"] = (1 + ism["benchmark"]).cumprod()
        results["price_action"] = {
            "in_sample": compute_metrics(ism),
            "out_of_sample": compute_metrics(oos),
            "full": compute_metrics(pa_results),
            "total_months": len(pa_results),
        }
        log.info(f"  IS:  {results['price_action']['in_sample']}")
        log.info(f"  OOS: {results['price_action']['out_of_sample']}")

    # --- Variant 3: Merged ---
    log.info("\n=== VARIANT 3: Merged (Trend + Price Action) ===")
    merged_results = run_merged(data)
    if not merged_results.empty:
        split = int(len(merged_results) * 0.70)
        oos = merged_results.iloc[split:].copy()
        oos["cumulative"] = (1 + oos["return"]).cumprod()
        oos["bench_cumulative"] = (1 + oos["benchmark"]).cumprod()
        ism = merged_results.iloc[:split].copy()
        ism["cumulative"] = (1 + ism["return"]).cumprod()
        ism["bench_cumulative"] = (1 + ism["benchmark"]).cumprod()
        results["merged"] = {
            "in_sample": compute_metrics(ism),
            "out_of_sample": compute_metrics(oos),
            "full": compute_metrics(merged_results),
            "total_months": len(merged_results),
        }
        log.info(f"  IS:  {results['merged']['in_sample']}")
        log.info(f"  OOS: {results['merged']['out_of_sample']}")

    # --- Summary table ---
    print(f"\n{'='*100}")
    print("PRICE ACTION BACKTEST — 3-WAY COMPARISON")
    print(f"{'='*100}")
    print(f"{'Variant':<30} {'OOS Sharpe':>10} {'OOS CAGR':>10} {'OOS MaxDD':>10} "
          f"{'Beat SPY':>10} {'Win Rate':>10} {'Months':>8}")
    print("─" * 100)

    for name, r in results.items():
        oos = r["out_of_sample"]
        print(
            f"{name:<30} "
            f"{oos.get('sharpe', 'N/A'):>10} "
            f"{str(oos.get('cagr', 'N/A'))+'%':>10} "
            f"{str(oos.get('max_drawdown', 'N/A'))+'%':>10} "
            f"{'Yes' if oos.get('beat_spy') else 'No':>10} "
            f"{str(oos.get('win_rate', 'N/A'))+'%':>10} "
            f"{r['total_months']:>8}"
        )

    print(f"\n{'─'*100}")
    print("IN-SAMPLE (training period):")
    print(f"{'─'*100}")
    for name, r in results.items():
        ism = r["in_sample"]
        print(
            f"{name:<30} "
            f"Sharpe={ism.get('sharpe', 'N/A'):>7}  "
            f"CAGR={str(ism.get('cagr', 'N/A'))+'%':>8}  "
            f"MaxDD={str(ism.get('max_drawdown', 'N/A'))+'%':>8}  "
            f"WinRate={str(ism.get('win_rate', 'N/A'))+'%':>7}"
        )

    # Log to DB
    log_agent_action(
        conn, "pa_backtester", "comparison_completed",
        outputs=results,
        reasoning="3-way comparison: trend-only vs price-action vs merged",
    )

    return results


def main():
    compare_all()


if __name__ == "__main__":
    main()
