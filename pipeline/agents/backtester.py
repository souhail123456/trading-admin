"""
Backtester Agent
----------------
Implements each strategy's exact rules on historical data.
Supports in-sample / out-of-sample splits and transaction costs.

Usage:
    python -m pipeline.agents.backtester --strategy-id 5
    python -m pipeline.agents.backtester --all
"""

import argparse
import json
import logging
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from pipeline.db import init_db, log_agent_action
from pipeline.agents.data_fetcher import fetch_for_strategy, BENCHMARK

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

# Default backtest config
DEFAULT_CONFIG = {
    "train_pct": 0.70,           # 70% in-sample, 30% out-of-sample
    "transaction_cost_bps": 5,   # 5bps per trade for liquid US equities
    "risk_free_rate": 0.02,      # for Sharpe calculation
}


# ---------------------------------------------------------------------------
# Strategy implementations
# ---------------------------------------------------------------------------

def momentum_rotational(
    data: dict[str, pd.DataFrame],
    lookback: int = 12,
    top_n: int = 3,
    benchmark_ticker: str = BENCHMARK,
    cost_bps: float = 5,
) -> pd.DataFrame:
    """
    Sector / Country / Stock momentum rotational strategy.

    Rules:
    - Each month, rank tickers by past `lookback` month return.
    - Go long the top `top_n`.
    - Equal weight, rebalance monthly.
    - Deduct transaction costs on each rebalance.
    """
    # Build monthly returns for all tickers (exclude benchmark)
    tickers = [t for t in data if t != benchmark_ticker]
    prices = {}
    for t in tickers:
        df = data[t]
        monthly = df["close"].resample("ME").last()
        prices[t] = monthly

    price_df = pd.DataFrame(prices).dropna(how="all")
    returns = price_df.pct_change()
    # Momentum signal: past `lookback` month cumulative return
    momentum = price_df.pct_change(lookback)

    # Benchmark
    if benchmark_ticker in data:
        bench_monthly = data[benchmark_ticker]["close"].resample("ME").last()
        bench_returns = bench_monthly.pct_change()
    else:
        bench_returns = pd.Series(0, index=returns.index)

    # Simulate
    portfolio_returns = []
    holdings_log = []
    trade_count = 0

    for i in range(lookback + 1, len(momentum)):
        date = momentum.index[i]
        mom_row = momentum.iloc[i - 1]  # signal from previous month (no lookahead)
        valid = mom_row.dropna()

        if len(valid) < top_n:
            portfolio_returns.append({"date": date, "return": 0.0, "benchmark": 0.0})
            continue

        # Rank and pick top_n
        ranked = valid.nlargest(top_n)
        selected = ranked.index.tolist()

        # Equal weight return for this month
        month_returns = returns.iloc[i][selected]
        raw_return = month_returns.mean()

        # Transaction cost: assume full turnover each month (conservative)
        cost = cost_bps / 10000 * 2  # buy + sell
        net_return = raw_return - cost
        trade_count += top_n

        bench_ret = bench_returns.iloc[i] if i < len(bench_returns) else 0.0

        portfolio_returns.append({
            "date": date,
            "return": net_return,
            "benchmark": bench_ret,
        })
        holdings_log.append({"date": str(date.date()), "holdings": selected})

    results = pd.DataFrame(portfolio_returns).set_index("date")
    results["cumulative"] = (1 + results["return"]).cumprod()
    results["bench_cumulative"] = (1 + results["benchmark"]).cumprod()

    return results, holdings_log, trade_count


def currency_momentum(
    data: dict[str, pd.DataFrame],
    lookback: int = 12,
    top_n: int = 3,
    cost_bps: float = 10,  # higher for FX
) -> pd.DataFrame:
    """
    Currency momentum: long top_n, short bottom_n by lookback-month return.
    """
    tickers = list(data.keys())
    prices = {}
    for t in tickers:
        df = data[t]
        monthly = df["close"].resample("ME").last()
        prices[t] = monthly

    price_df = pd.DataFrame(prices).dropna(how="all")
    returns = price_df.pct_change()
    momentum = price_df.pct_change(lookback)

    portfolio_returns = []
    holdings_log = []
    trade_count = 0

    for i in range(lookback + 1, len(momentum)):
        date = momentum.index[i]
        mom_row = momentum.iloc[i - 1]
        valid = mom_row.dropna()

        if len(valid) < top_n * 2:
            portfolio_returns.append({"date": date, "return": 0.0, "benchmark": 0.0})
            continue

        # Long top, short bottom
        longs = valid.nlargest(top_n).index.tolist()
        shorts = valid.nsmallest(top_n).index.tolist()

        long_ret = returns.iloc[i][longs].mean()
        short_ret = returns.iloc[i][shorts].mean()
        raw_return = (long_ret - short_ret) / 2  # dollar neutral

        cost = cost_bps / 10000 * 2
        net_return = raw_return - cost
        trade_count += top_n * 2

        portfolio_returns.append({
            "date": date,
            "return": net_return,
            "benchmark": 0.0,  # no equity benchmark for FX
        })
        holdings_log.append({
            "date": str(date.date()),
            "longs": longs,
            "shorts": shorts,
        })

    results = pd.DataFrame(portfolio_returns).set_index("date")
    results["cumulative"] = (1 + results["return"]).cumprod()
    results["bench_cumulative"] = 1.0

    return results, holdings_log, trade_count


def short_term_reversal(
    data: dict[str, pd.DataFrame],
    lookback: int = 1,
    top_n: int = 10,
    benchmark_ticker: str = BENCHMARK,
    cost_bps: float = 10,
) -> tuple:
    """
    Short-term reversal: buy weekly losers, short weekly winners.
    Rebalance weekly.
    """
    tickers = [t for t in data if t != benchmark_ticker]
    prices = {}
    for t in tickers:
        prices[t] = data[t]["close"].resample("W-FRI").last()

    price_df = pd.DataFrame(prices).dropna(how="all")
    returns = price_df.pct_change()

    if benchmark_ticker in data:
        bench_weekly = data[benchmark_ticker]["close"].resample("W-FRI").last()
        bench_returns = bench_weekly.pct_change()
    else:
        bench_returns = pd.Series(0, index=returns.index)

    portfolio_returns = []
    holdings_log = []
    trade_count = 0

    for i in range(lookback + 1, len(returns)):
        date = returns.index[i]
        prev_ret = returns.iloc[i - 1].dropna()

        if len(prev_ret) < top_n * 2:
            portfolio_returns.append({"date": date, "return": 0.0, "benchmark": 0.0})
            continue

        # Buy losers, short winners
        longs = prev_ret.nsmallest(top_n).index.tolist()
        shorts = prev_ret.nlargest(top_n).index.tolist()

        long_ret = returns.iloc[i][longs].mean()
        short_ret = returns.iloc[i][shorts].mean()
        raw_return = (long_ret - short_ret) / 2

        cost = cost_bps / 10000 * 2
        net_return = raw_return - cost
        trade_count += top_n * 2

        bench_ret = bench_returns.iloc[i] if i < len(bench_returns) else 0.0
        portfolio_returns.append({"date": date, "return": net_return, "benchmark": bench_ret})
        holdings_log.append({"date": str(date.date()), "longs": longs, "shorts": shorts})

    results = pd.DataFrame(portfolio_returns).set_index("date")
    if results.empty:
        return results, [], 0
    results["cumulative"] = (1 + results["return"]).cumprod()
    results["bench_cumulative"] = (1 + results["benchmark"]).cumprod()
    return results, holdings_log, trade_count


def trend_following(
    data: dict[str, pd.DataFrame],
    sma_period: int = 200,
    atr_period: int = 10,
    top_n: int = 5,
    benchmark_ticker: str = BENCHMARK,
    cost_bps: float = 5,
) -> tuple:
    """
    Trend-following: buy when price > SMA, use ATR trailing stop.
    Monthly rebalance of the portfolio.
    """
    tickers = [t for t in data if t != benchmark_ticker]

    # Compute signals daily, rebalance monthly
    signals = {}
    for t in tickers:
        df = data[t].copy()
        df["sma"] = df["close"].rolling(sma_period).mean()
        df["atr"] = (df["high"] - df["low"]).rolling(atr_period).mean()
        df["above_sma"] = (df["close"] > df["sma"]).astype(int)
        # Momentum score for ranking: distance above SMA
        df["trend_strength"] = (df["close"] - df["sma"]) / df["sma"]
        signals[t] = df[["close", "above_sma", "trend_strength"]].resample("ME").last()

    # Monthly returns
    monthly_prices = {}
    for t in tickers:
        monthly_prices[t] = data[t]["close"].resample("ME").last()
    price_df = pd.DataFrame(monthly_prices).dropna(how="all")
    returns = price_df.pct_change()

    if benchmark_ticker in data:
        bench_monthly = data[benchmark_ticker]["close"].resample("ME").last()
        bench_returns = bench_monthly.pct_change()
    else:
        bench_returns = pd.Series(0, index=returns.index)

    portfolio_returns = []
    holdings_log = []
    trade_count = 0
    start_idx = max(sma_period // 20, 12)  # ~sma_period trading days / ~20 days per month

    for i in range(start_idx, len(returns)):
        date = returns.index[i]

        # Find tickers above SMA with strongest trend
        candidates = []
        for t in tickers:
            if t in signals and date in signals[t].index:
                row = signals[t].loc[date]
                if row["above_sma"] > 0:
                    candidates.append((t, row["trend_strength"]))

        if not candidates:
            # No trend — go to cash
            bench_ret = bench_returns.iloc[i] if i < len(bench_returns) else 0.0
            portfolio_returns.append({"date": date, "return": 0.0, "benchmark": bench_ret})
            continue

        # Pick top_n by trend strength
        candidates.sort(key=lambda x: x[1], reverse=True)
        selected = [c[0] for c in candidates[:top_n]]

        month_returns = returns.iloc[i][selected]
        raw_return = month_returns.mean()
        cost = cost_bps / 10000 * 2
        net_return = raw_return - cost
        trade_count += len(selected)

        bench_ret = bench_returns.iloc[i] if i < len(bench_returns) else 0.0
        portfolio_returns.append({"date": date, "return": net_return, "benchmark": bench_ret})
        holdings_log.append({"date": str(date.date()), "holdings": selected})

    results = pd.DataFrame(portfolio_returns).set_index("date")
    if results.empty:
        return results, [], 0
    results["cumulative"] = (1 + results["return"]).cumprod()
    results["bench_cumulative"] = (1 + results["benchmark"]).cumprod()
    return results, holdings_log, trade_count


def low_volatility(
    data: dict[str, pd.DataFrame],
    vol_lookback: int = 52,
    top_n: int = 3,
    benchmark_ticker: str = BENCHMARK,
    cost_bps: float = 5,
) -> tuple:
    """
    Low volatility factor: buy lowest-volatility stocks, rebalance monthly.
    """
    tickers = [t for t in data if t != benchmark_ticker]
    weekly_prices = {}
    for t in tickers:
        weekly_prices[t] = data[t]["close"].resample("W-FRI").last()

    price_df = pd.DataFrame(weekly_prices).dropna(how="all")
    weekly_returns = price_df.pct_change()

    # Rolling volatility
    vol = weekly_returns.rolling(vol_lookback).std()

    # Monthly rebalance
    monthly_vol = vol.resample("ME").last()
    monthly_prices_all = {}
    for t in tickers:
        monthly_prices_all[t] = data[t]["close"].resample("ME").last()
    mp_df = pd.DataFrame(monthly_prices_all).dropna(how="all")
    monthly_returns = mp_df.pct_change()

    if benchmark_ticker in data:
        bench = data[benchmark_ticker]["close"].resample("ME").last().pct_change()
    else:
        bench = pd.Series(0, index=monthly_returns.index)

    portfolio_returns = []
    holdings_log = []
    trade_count = 0

    for i in range(vol_lookback // 4 + 1, len(monthly_vol)):
        date = monthly_vol.index[i]
        vol_row = monthly_vol.iloc[i - 1].dropna()

        if len(vol_row) < top_n:
            portfolio_returns.append({"date": date, "return": 0.0, "benchmark": 0.0})
            continue

        # Pick lowest volatility
        selected = vol_row.nsmallest(top_n).index.tolist()
        month_ret = monthly_returns.iloc[i][selected].mean() if date in monthly_returns.index else 0.0
        cost = cost_bps / 10000 * 2
        net_return = month_ret - cost
        trade_count += top_n

        bench_ret = bench.iloc[i] if i < len(bench) else 0.0
        portfolio_returns.append({"date": date, "return": net_return, "benchmark": bench_ret})
        holdings_log.append({"date": str(date.date()), "holdings": selected})

    results = pd.DataFrame(portfolio_returns).set_index("date")
    if results.empty:
        return results, [], 0
    results["cumulative"] = (1 + results["return"]).cumprod()
    results["bench_cumulative"] = (1 + results["benchmark"]).cumprod()
    return results, holdings_log, trade_count


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def compute_metrics(results: pd.DataFrame, risk_free: float = 0.02) -> dict:
    """Compute performance metrics from a returns DataFrame."""
    rets = results["return"].dropna()
    bench = results["benchmark"].dropna()

    if len(rets) < 12:
        return {"error": "insufficient data"}

    # Annualized return
    total_return = results["cumulative"].iloc[-1]
    years = len(rets) / 12
    cagr = (total_return ** (1 / years) - 1) * 100 if years > 0 else 0

    # Volatility
    annual_vol = rets.std() * np.sqrt(12) * 100

    # Sharpe
    excess = rets.mean() * 12 - risk_free
    sharpe = excess / (rets.std() * np.sqrt(12)) if rets.std() > 0 else 0

    # Max drawdown
    cum = results["cumulative"]
    running_max = cum.cummax()
    drawdown = (cum - running_max) / running_max
    max_dd = drawdown.min() * 100

    # Win rate
    win_rate = (rets > 0).mean() * 100

    # SPY comparison
    if bench.sum() != 0:
        bench_total = results["bench_cumulative"].iloc[-1]
        bench_cagr = (bench_total ** (1 / years) - 1) * 100 if years > 0 else 0
        beat_spy = 1 if cagr > bench_cagr else 0
    else:
        bench_cagr = None
        beat_spy = None

    return {
        "cagr": round(cagr, 2),
        "annual_volatility": round(annual_vol, 2),
        "sharpe": round(sharpe, 3),
        "max_drawdown": round(max_dd, 2),
        "win_rate": round(win_rate, 1),
        "total_months": len(rets),
        "total_return": round((total_return - 1) * 100, 2),
        "benchmark_cagr": round(bench_cagr, 2) if bench_cagr is not None else None,
        "beat_spy": beat_spy,
    }


# ---------------------------------------------------------------------------
# Run backtest
# ---------------------------------------------------------------------------

def _safe_int(val, default=3):
    """Extract int from param value that may be string like '10-12' or '5'."""
    if val is None:
        return default
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    try:
        return int(str(val).split("-")[0].split()[0])
    except (ValueError, IndexError):
        return default


def select_strategy_impl(strategy: dict):
    """Pick the right backtest function based on strategy type."""
    name = strategy["name"].lower()
    params = json.loads(strategy["parameters"]) if isinstance(strategy["parameters"], str) else strategy["parameters"]

    # --- Currency momentum (L/S) ---
    if "currenc" in name:
        lookback = _safe_int(params.get("lookback_period", 12))
        top_n = _safe_int(params.get("number_of_long_positions", 3))
        return currency_momentum, {"lookback": lookback, "top_n": top_n, "cost_bps": 10}

    # --- Short-term reversal ---
    if "reversal" in name and ("short term" in name or "short-term" in name):
        lookback = _safe_int(params.get("lookback_period", params.get("ranking_period_weeks", 1)))
        top_n = _safe_int(params.get("number_of_stocks", params.get("number_of_long_positions", 10)))
        return short_term_reversal, {"lookback": lookback, "top_n": min(top_n, 5)}

    # --- Long-term reversal (international ETFs, 3-year) ---
    if "reversal" in name and "international" in name:
        return momentum_rotational, {"lookback": 36, "top_n": 4}

    # --- Reversal + momentum + volatility combo ---
    if "reversal" in name and "momentum" in name:
        return short_term_reversal, {"lookback": 1, "top_n": 5, "cost_bps": 10}

    # --- Post-earnings / earnings strategies ---
    if "earning" in name:
        # Approximate with short-term reversal (earnings drift is a reversal effect)
        return short_term_reversal, {"lookback": 1, "top_n": 5, "cost_bps": 10}

    # --- Trend-following ---
    if "trend" in name:
        sma = _safe_int(params.get("sma_period", params.get("moving_average_period", 200)))
        top_n = _safe_int(params.get("number_of_stocks", 5))
        return trend_following, {"sma_period": sma, "top_n": top_n}

    # --- Low volatility ---
    if "volatil" in name and ("low" in name or "risk premium" in name):
        top_n = _safe_int(params.get("number_of_stocks", params.get("decile_size", 3)))
        return low_volatility, {"vol_lookback": 52, "top_n": top_n}

    # --- Seasonality ---
    if "season" in name:
        return momentum_rotational, {"lookback": 12, "top_n": 5}

    # --- Alpha cloning / 13F ---
    if "alpha" in name or "13f" in name:
        return momentum_rotational, {"lookback": 3, "top_n": 5}

    # --- Default: momentum rotational ---
    lookback = _safe_int(params.get("momentum_lookback_period", params.get("lookback_period", 12)))
    top_n = _safe_int(params.get("number_of_etfs_to_pick",
                                  params.get("selection_count",
                                             params.get("number_of_traded_instruments", 3))))
    return momentum_rotational, {"lookback": lookback, "top_n": top_n}


def run_backtest(
    strategy: dict,
    data: dict[str, pd.DataFrame],
    conn: sqlite3.Connection,
    config: dict | None = None,
) -> dict:
    """
    Run full backtest with in-sample / out-of-sample split.
    """
    config = config or DEFAULT_CONFIG
    impl_fn, impl_params = select_strategy_impl(strategy)

    log.info(f"Backtesting [{strategy['id']}] {strategy['name']}")
    log.info(f"  Params: {impl_params}")

    # Run on full data first
    results, holdings, trade_count = impl_fn(data, **impl_params)

    if results.empty:
        log.error("  No results — insufficient data")
        return {}

    # Split into in-sample / out-of-sample
    split_idx = int(len(results) * config["train_pct"])
    in_sample = results.iloc[:split_idx].copy()
    out_sample = results.iloc[split_idx:].copy()

    # Recompute cumulative for each split
    in_sample["cumulative"] = (1 + in_sample["return"]).cumprod()
    in_sample["bench_cumulative"] = (1 + in_sample["benchmark"]).cumprod()
    out_sample["cumulative"] = (1 + out_sample["return"]).cumprod()
    out_sample["bench_cumulative"] = (1 + out_sample["benchmark"]).cumprod()

    full_metrics = compute_metrics(results)
    is_metrics = compute_metrics(in_sample)
    oos_metrics = compute_metrics(out_sample)

    split_date = results.index[split_idx].strftime("%Y-%m-%d") if split_idx < len(results) else "N/A"

    log.info(f"  Full period: {full_metrics}")
    log.info(f"  In-sample:   {is_metrics}")
    log.info(f"  Out-of-sample: {oos_metrics}")

    # Store results
    for run_type, metrics in [("in_sample", is_metrics), ("out_of_sample", oos_metrics)]:
        conn.execute(
            """INSERT INTO backtest_runs
               (strategy_id, run_type, parameters, data_start, data_end,
                split_point, sharpe, cagr, max_drawdown, win_rate,
                total_trades, transaction_cost_bps, beat_spy, report)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                strategy["id"], run_type, json.dumps(impl_params),
                results.index[0].strftime("%Y-%m-%d"),
                results.index[-1].strftime("%Y-%m-%d"),
                split_date,
                metrics.get("sharpe"), metrics.get("cagr"), metrics.get("max_drawdown"),
                metrics.get("win_rate"), trade_count,
                config["transaction_cost_bps"], metrics.get("beat_spy"),
                json.dumps(metrics),
            ),
        )
    conn.commit()

    log_agent_action(
        conn, "backtester", "backtest_completed",
        inputs={"strategy_id": strategy["id"], "params": impl_params},
        outputs={
            "full": full_metrics,
            "in_sample": is_metrics,
            "out_of_sample": oos_metrics,
            "split_date": split_date,
        },
        strategy_id=strategy["id"],
    )

    return {
        "strategy_id": strategy["id"],
        "name": strategy["name"],
        "full": full_metrics,
        "in_sample": is_metrics,
        "out_of_sample": oos_metrics,
        "split_date": split_date,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def backtest_all(strategy_id: int | None = None, db_path: str | None = None) -> list[dict]:
    conn = init_db(db_path)
    results = []

    if strategy_id:
        rows = conn.execute("SELECT * FROM strategies WHERE id = ?", (strategy_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM strategies WHERE status = 'candidate' AND id >= 5 ORDER BY id"
        ).fetchall()

    strategies = [dict(r) for r in rows]

    for strat in strategies:
        data = fetch_for_strategy(strat, conn)
        if not data:
            log.error(f"  No data for [{strat['id']}] — skipping")
            continue

        result = run_backtest(strat, data, conn)
        if result:
            results.append(result)
        print()

    # Summary table
    if results:
        print(f"\n{'='*90}")
        print(f"BACKTEST RESULTS SUMMARY")
        print(f"{'='*90}")
        print(f"{'Strategy':<45} {'OOS Sharpe':>10} {'OOS CAGR':>10} {'OOS MaxDD':>10} {'Beat SPY':>10}")
        print("─" * 90)
        for r in results:
            oos = r["out_of_sample"]
            print(
                f"{r['name']:<45} "
                f"{oos.get('sharpe', 'N/A'):>10} "
                f"{str(oos.get('cagr', 'N/A'))+'%':>10} "
                f"{str(oos.get('max_drawdown', 'N/A'))+'%':>10} "
                f"{'Yes' if oos.get('beat_spy') else 'No':>10}"
            )

    return results


def main():
    parser = argparse.ArgumentParser(description="Backtester — run strategy backtests")
    parser.add_argument("--strategy-id", type=int, default=None)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--db", type=str, default=None)
    args = parser.parse_args()

    sid = args.strategy_id if not args.all else None
    backtest_all(strategy_id=sid, db_path=args.db)


if __name__ == "__main__":
    main()
