"""
FX Backtester
-------------
Compares three strategy variants on currency pair data:
  1. Trend-following alone (SMA-200 filter on FX)
  2. Price action alone (candlestick + structure patterns on FX)
  3. Merged: trend filter + price action entry timing

Uses 10 major FX pairs, 20 years of data, 70/30 IS/OOS split.

Usage:
    python3 -m pipeline.agents.fx_backtester
"""

import json
import logging

import numpy as np
import pandas as pd

from pipeline.db import init_db, log_agent_action
from pipeline.agents.data_fetcher import fetch_ohlcv, CURRENCY_PAIRS
from pipeline.agents.price_action import detect_all_patterns
from pipeline.agents.backtester import compute_metrics
from pipeline.agents.fx_signal_generator import macd_histogram, rsi

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Variant 1: Trend-following on FX
# ---------------------------------------------------------------------------

def fx_trend_following(
    data: dict[str, pd.DataFrame],
    sma_period: int = 200,
    top_n: int = 3,
    cost_bps: float = 3,  # FX spreads are tight
    macd_filter: bool = False,
    rsi_filter: bool = False,
    rsi_overbought: float = 70,
) -> pd.DataFrame:
    """
    FX trend-following: go long pairs above SMA-200, rank by trend strength.
    Monthly rebalance. No shorting (simpler for $100 account).

    Entry-confirmation filters (long-only mirror of fx_trend_signals):
      - macd_filter: require MACD histogram > 0 at month-end (momentum agrees)
      - rsi_filter:  skip pairs with RSI-14 >= rsi_overbought (exhaustion guard)
    """
    tickers = list(data.keys())

    # Compute daily signals
    signals = {}
    for t in tickers:
        df = data[t].copy()
        if len(df) < sma_period + 50:
            continue
        df["sma"] = df["close"].rolling(sma_period).mean()
        df["above_sma"] = (df["close"] > df["sma"]).astype(int)
        df["trend_strength"] = (df["close"] - df["sma"]) / df["sma"]
        df["macd_hist"] = macd_histogram(df["close"])
        df["rsi"] = rsi(df["close"])
        signals[t] = df[["close", "above_sma", "trend_strength", "macd_hist", "rsi"]].resample("ME").last()

    # Monthly returns
    monthly_prices = {}
    for t in tickers:
        if t in signals:
            monthly_prices[t] = data[t]["close"].resample("ME").last()
    price_df = pd.DataFrame(monthly_prices).dropna(how="all")
    returns = price_df.pct_change()

    portfolio_returns = []
    start_idx = max(sma_period // 20, 12)

    for i in range(start_idx, len(returns)):
        date = returns.index[i]

        candidates = []
        for t in signals:
            if date in signals[t].index:
                row = signals[t].loc[date]
                if row["above_sma"] <= 0:
                    continue
                # Entry-confirmation filters (long side)
                if macd_filter and not (row["macd_hist"] > 0):
                    continue
                if rsi_filter and row["rsi"] >= rsi_overbought:
                    continue
                candidates.append((t, row["trend_strength"]))

        if not candidates:
            portfolio_returns.append({"date": date, "return": 0.0, "benchmark": 0.0, "trades_opened": 0})
            continue

        candidates.sort(key=lambda x: x[1], reverse=True)
        selected = [c[0] for c in candidates[:top_n]]

        month_returns = returns.iloc[i][selected]
        raw_return = month_returns.mean()
        cost = cost_bps / 10000 * 2
        net_return = raw_return - cost

        portfolio_returns.append({"date": date, "return": net_return, "benchmark": 0.0, "trades_opened": len(selected)})

    results = pd.DataFrame(portfolio_returns).set_index("date")
    if results.empty:
        return results
    results["cumulative"] = (1 + results["return"]).cumprod()
    results["bench_cumulative"] = 1.0
    return results


# ---------------------------------------------------------------------------
# Variant 2: Price action on FX
# ---------------------------------------------------------------------------

def fx_price_action(
    data: dict[str, pd.DataFrame],
    min_score: int = 2,
    max_positions: int = 3,
    hold_days: int = 15,  # FX moves faster, shorter hold
    stop_loss: float = -0.03,  # 3% stop (tighter for FX)
) -> pd.DataFrame:
    """
    Pure price action on FX pairs.
    Enter on bullish patterns, exit on bearish or stop loss.
    """
    tickers = list(data.keys())

    patterns = {}
    for t in tickers:
        if len(data[t]) < 200:
            continue
        patterns[t] = detect_all_patterns(data[t])

    if not patterns:
        return pd.DataFrame()

    all_dates = sorted(set().union(*(p.index for p in patterns.values())))
    all_dates = pd.DatetimeIndex(all_dates)

    positions = {}
    daily_returns = []

    for i, date in enumerate(all_dates):
        if i == 0:
            continue
        prev_date = all_dates[i - 1]

        pos_returns = []
        to_exit = []

        for ticker, pos in positions.items():
            if ticker not in data or date not in data[ticker].index:
                continue
            if prev_date not in data[ticker].index:
                continue

            current = data[ticker].loc[date, "close"]
            prev = data[ticker].loc[prev_date, "close"]
            day_ret = (current - prev) / prev
            pos_returns.append(day_ret)

            total_ret = (current - pos["entry_price"]) / pos["entry_price"]
            days_held = (date - pos["entry_date"]).days

            exit_signal = False
            if ticker in patterns and date in patterns[ticker].index:
                exit_signal = patterns[ticker].loc[date, "bear_signal"] > 0

            if total_ret <= stop_loss or days_held >= hold_days or exit_signal:
                to_exit.append(ticker)

        port_ret = np.mean(pos_returns) if pos_returns else 0.0

        for t in to_exit:
            del positions[t]

        slots = max_positions - len(positions)
        if slots > 0:
            candidates = []
            for t in tickers:
                if t in positions or t not in patterns:
                    continue
                if date not in patterns[t].index:
                    continue
                score = patterns[t].loc[date, "bull_score"]
                if score >= min_score:
                    candidates.append((t, score))

            candidates.sort(key=lambda x: x[1], reverse=True)
            entered = candidates[:slots]
            for t, _ in entered:
                if date in data[t].index:
                    positions[t] = {
                        "entry_date": date,
                        "entry_price": data[t].loc[date, "close"],
                    }
        else:
            entered = []

        daily_returns.append({"date": date, "return": port_ret, "benchmark": 0.0, "trades_opened": len(entered)})

    if not daily_returns:
        return pd.DataFrame()

    results = pd.DataFrame(daily_returns).set_index("date")
    monthly = results.resample("ME").agg({
        "return": lambda x: (1 + x).prod() - 1,
        "benchmark": lambda x: (1 + x).prod() - 1,
        "trades_opened": "sum",
    })
    monthly["cumulative"] = (1 + monthly["return"]).cumprod()
    monthly["bench_cumulative"] = 1.0
    return monthly


# ---------------------------------------------------------------------------
# Variant 3: Merged (trend filter + price action entry on FX)
# ---------------------------------------------------------------------------

def fx_merged(
    data: dict[str, pd.DataFrame],
    sma_period: int = 200,
    top_n: int = 3,
    min_pa_score: int = 1,
    hold_days: int = 15,
    stop_loss: float = -0.03,
) -> pd.DataFrame:
    """
    Merged: trend filter picks which pairs to consider,
    price action times the entry.
    """
    tickers = list(data.keys())

    trend_signals = {}
    for t in tickers:
        df = data[t].copy()
        if len(df) < sma_period + 50:
            continue
        df["sma"] = df["close"].rolling(sma_period).mean()
        df["above_sma"] = df["close"] > df["sma"]
        df["trend_strength"] = (df["close"] - df["sma"]) / df["sma"]
        trend_signals[t] = df

    patterns = {}
    for t in trend_signals:
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

        pos_returns = []
        to_exit = []

        for ticker, pos in positions.items():
            if date not in data[ticker].index or prev_date not in data[ticker].index:
                continue

            current = data[ticker].loc[date, "close"]
            prev = data[ticker].loc[prev_date, "close"]
            day_ret = (current - prev) / prev
            pos_returns.append(day_ret)

            total_ret = (current - pos["entry_price"]) / pos["entry_price"]
            days_held = (date - pos["entry_date"]).days

            below_sma = False
            if ticker in trend_signals and date in trend_signals[ticker].index:
                below_sma = not trend_signals[ticker].loc[date, "above_sma"]

            bear_pa = False
            if ticker in patterns and date in patterns[ticker].index:
                bear_pa = patterns[ticker].loc[date, "bear_signal"] > 0

            force_exit = total_ret <= stop_loss or below_sma or bear_pa
            time_exit = days_held >= hold_days and not (
                ticker in trend_signals
                and date in trend_signals[ticker].index
                and trend_signals[ticker].loc[date, "above_sma"]
            )

            if force_exit or time_exit:
                to_exit.append(ticker)

        port_ret = np.mean(pos_returns) if pos_returns else 0.0

        for t in to_exit:
            del positions[t]

        slots = top_n - len(positions)
        if slots > 0:
            trend_candidates = []
            for t in tickers:
                if t in positions or t not in trend_signals:
                    continue
                if date not in trend_signals[t].index:
                    continue
                row = trend_signals[t].loc[date]
                if row["above_sma"]:
                    trend_candidates.append((t, row["trend_strength"]))

            trend_candidates.sort(key=lambda x: x[1], reverse=True)
            top_trend = [c[0] for c in trend_candidates[:top_n]]

            for t in top_trend:
                if len(positions) >= top_n or t in positions:
                    continue
                if t not in patterns or date not in patterns[t].index:
                    continue
                if patterns[t].loc[date, "bull_score"] >= min_pa_score:
                    if date in data[t].index:
                        positions[t] = {
                            "entry_date": date,
                            "entry_price": data[t].loc[date, "close"],
                        }

        daily_returns.append({"date": date, "return": port_ret, "benchmark": 0.0})

    if not daily_returns:
        return pd.DataFrame()

    results = pd.DataFrame(daily_returns).set_index("date")
    monthly = results.resample("ME").agg({
        "return": lambda x: (1 + x).prod() - 1,
        "benchmark": lambda x: (1 + x).prod() - 1,
    })
    monthly["cumulative"] = (1 + monthly["return"]).cumprod()
    monthly["bench_cumulative"] = 1.0
    return monthly


# ---------------------------------------------------------------------------
# Compare all three
# ---------------------------------------------------------------------------

def _split_and_metrics(results: pd.DataFrame, name: str) -> dict | None:
    if results.empty:
        log.warning(f"  {name}: no results")
        return None

    split = int(len(results) * 0.70)
    ism = results.iloc[:split].copy()
    ism["cumulative"] = (1 + ism["return"]).cumprod()
    ism["bench_cumulative"] = 1.0
    oos = results.iloc[split:].copy()
    oos["cumulative"] = (1 + oos["return"]).cumprod()
    oos["bench_cumulative"] = 1.0

    has_trades = "trades_opened" in results.columns
    split_date = results.index[split].strftime("%Y-%m-%d") if split < len(results) else None

    return {
        "in_sample": compute_metrics(ism),
        "out_of_sample": compute_metrics(oos),
        "full": compute_metrics(results),
        "total_months": len(results),
        "data_start": results.index[0].strftime("%Y-%m-%d"),
        "data_end": results.index[-1].strftime("%Y-%m-%d"),
        "split_date": split_date,
        "trades_in_sample": int(ism["trades_opened"].sum()) if has_trades else None,
        "trades_out_of_sample": int(oos["trades_opened"].sum()) if has_trades else None,
    }


def _store_backtest_run(
    conn, strategy_id: int, variant_name: str, params: dict, m: dict,
) -> None:
    """Persist in-sample / out-of-sample metrics for a variant into backtest_runs,
    keyed to the given strategy_id (only called for variants that map to a real
    strategy row, e.g. fx_trend -> 100, fx_price_action -> 101)."""
    for run_type, metrics, trade_count in [
        ("in_sample", m["in_sample"], m.get("trades_in_sample")),
        ("out_of_sample", m["out_of_sample"], m.get("trades_out_of_sample")),
    ]:
        if "error" in metrics:
            log.warning(f"  Skipping DB store for {variant_name} {run_type}: {metrics['error']}")
            continue
        conn.execute(
            """INSERT INTO backtest_runs
               (strategy_id, run_type, parameters, data_start, data_end,
                split_point, sharpe, cagr, max_drawdown, win_rate,
                total_trades, transaction_cost_bps, beat_spy, report)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                strategy_id, run_type, json.dumps(params),
                m["data_start"], m["data_end"], m["split_date"],
                metrics.get("sharpe"), metrics.get("cagr"), metrics.get("max_drawdown"),
                metrics.get("win_rate"), trade_count,
                params.get("cost_bps"), metrics.get("beat_spy"),
                json.dumps(metrics),
            ),
        )
    conn.commit()
    log.info(f"  Stored {variant_name} in_sample/out_of_sample rows in backtest_runs (strategy_id={strategy_id})")


# Strategy IDs 100/101 in the `strategies` table — parameters mirrored here
# so backtest_runs rows can be tied back to the live paper-trading strategies.
FX_TREND_STRATEGY_ID = 100
FX_TREND_PARAMS = {"sma_period": 200, "top_n": 3, "cost_bps": 3}

FX_PA_STRATEGY_ID = 101
FX_PA_PARAMS = {"min_score": 2, "max_positions": 3, "hold_days": 15, "stop_loss": -0.03}


def compare_all(db_path: str | None = None) -> dict:
    conn = init_db(db_path)

    log.info("Loading FX data...")
    tickers = list(CURRENCY_PAIRS.keys())
    data = fetch_ohlcv(tickers, start="2005-01-01")
    log.info(f"Loaded {len(data)} FX pairs")

    results = {}

    # Variant 1
    log.info("\n=== VARIANT 1: FX Trend-Following ===")
    r = fx_trend_following(data, **FX_TREND_PARAMS)
    m = _split_and_metrics(r, "trend")
    if m:
        results["fx_trend"] = m
        log.info(f"  IS:  {m['in_sample']}")
        log.info(f"  OOS: {m['out_of_sample']}")
        _store_backtest_run(conn, FX_TREND_STRATEGY_ID, "fx_trend", FX_TREND_PARAMS, m)

    # Variant 2
    log.info("\n=== VARIANT 2: FX Price Action ===")
    r = fx_price_action(data, **FX_PA_PARAMS)
    m = _split_and_metrics(r, "price_action")
    if m:
        results["fx_price_action"] = m
        log.info(f"  IS:  {m['in_sample']}")
        log.info(f"  OOS: {m['out_of_sample']}")
        _store_backtest_run(conn, FX_PA_STRATEGY_ID, "fx_price_action", FX_PA_PARAMS, m)

    # Variant 3
    log.info("\n=== VARIANT 3: FX Merged (Trend + PA) ===")
    r = fx_merged(data)
    m = _split_and_metrics(r, "merged")
    if m:
        results["fx_merged"] = m
        log.info(f"  IS:  {m['in_sample']}")
        log.info(f"  OOS: {m['out_of_sample']}")

    # Summary
    print(f"\n{'='*100}")
    print("FX STRATEGY BACKTEST — 3-WAY COMPARISON (10 currency pairs, 2005-2026)")
    print(f"{'='*100}")
    print(f"{'Variant':<25} {'OOS Sharpe':>10} {'OOS CAGR':>10} {'OOS MaxDD':>10} "
          f"{'IS Sharpe':>10} {'IS CAGR':>10} {'Win Rate':>10} {'Months':>8}")
    print("─" * 100)

    for name, r in results.items():
        oos = r["out_of_sample"]
        ism = r["in_sample"]
        print(
            f"{name:<25} "
            f"{oos.get('sharpe', 'N/A'):>10} "
            f"{str(oos.get('cagr', 'N/A'))+'%':>10} "
            f"{str(oos.get('max_drawdown', 'N/A'))+'%':>10} "
            f"{ism.get('sharpe', 'N/A'):>10} "
            f"{str(ism.get('cagr', 'N/A'))+'%':>10} "
            f"{str(oos.get('win_rate', 'N/A'))+'%':>10} "
            f"{r['total_months']:>8}"
        )

    log_agent_action(
        conn, "fx_backtester", "comparison_completed",
        outputs=results,
        reasoning="3-way FX comparison: trend vs price-action vs merged on 10 currency pairs",
    )

    return results


# ---------------------------------------------------------------------------
# BACKTEST-ONLY broader universe (analysis only — NOT part of the live traded set)
# ---------------------------------------------------------------------------
# Used ONLY by compare_universe() to test the thesis that the SMA-200 trend
# strategy trends more cleanly on metals / equity indices / energy than on spot
# FX majors. These tickers are deliberately kept OUT of
# data_fetcher.CURRENCY_PAIRS, so they can NEVER leak into live signal
# generation — fx_signal_generator only ever iterates CURRENCY_PAIRS.
#
# CARRY NOTE: fx_trend_following() ranks candidates purely by `trend_strength`
# (the FX carry term lives only in the LIVE fx_trend_signals composite, not in
# the backtester). So for these non-FX instruments there is literally nothing
# to zero out — they carry no FX interest-rate differential and the backtest
# ranking here is already pure trend_strength. Verified: no carry is fed in.
#
# (label, asset_class). Futures/cash-index tickers chosen over ETF proxies for
# clean 2006-2026 history with no roll/tracking-error distortion.
BACKTEST_EXTRA_UNIVERSE: dict[str, tuple[str, str]] = {
    "GC=F":  ("Gold (fut)",     "metals"),
    "SI=F":  ("Silver (fut)",   "metals"),
    "^GSPC": ("S&P 500",        "indices"),
    "^NDX":  ("Nasdaq 100",     "indices"),
    "CL=F":  ("WTI Crude (fut)", "energy"),
}
FX_CLASS = "fx"


def compare_universe(db_path: str | None = None, start: str = "2006-01-01") -> dict:
    """Backtest the CURRENT LIVE fx-trend config (SMA-200, MACD ON, RSI OFF)
    across the existing FX pairs AND a broader backtest-only universe of
    gold / silver / equity indices / oil.

    Each instrument is run in ISOLATION so we get a clean per-instrument trend
    Sharpe (the strategy holds that instrument in any month it is above its own
    SMA-200 with MACD histogram > 0, else sits flat). Metrics are then averaged
    by asset class. Same strategy, same cost assumption (3 bps) everywhere so
    the ONLY thing that varies is the instrument — an apples-to-apples test of
    'are we trend-following the wrong universe?'.

    ANALYSIS ONLY — this touches nothing in the live traded universe.
    """
    init_db(db_path)

    fx_tickers = list(CURRENCY_PAIRS.keys())
    extra_tickers = list(BACKTEST_EXTRA_UNIVERSE.keys())

    log.info("Loading FX + broader universe for universe comparison...")
    data = fetch_ohlcv(fx_tickers + extra_tickers, start=start)
    log.info(f"Loaded {len(data)} instruments")

    # Current live config: MACD ON, RSI OFF, SMA-200. cost_bps held equal across
    # all instruments (3 bps) so the strategy is identical and only the
    # instrument changes. NB: real costs differ (FX tightest; index/oil futures
    # slightly wider) — see caveats in the report.
    live_cfg = {"sma_period": 200, "top_n": 3, "cost_bps": 3,
                "macd_filter": True, "rsi_filter": False}

    labels: dict[str, str] = {}
    classes: dict[str, str] = {}
    for t in fx_tickers:
        labels[t], classes[t] = CURRENCY_PAIRS[t], FX_CLASS
    for t in extra_tickers:
        labels[t], classes[t] = BACKTEST_EXTRA_UNIVERSE[t]

    rows = []
    for t in fx_tickers + extra_tickers:
        if t not in data:
            log.warning(f"  {t}: not loaded, skipping")
            continue
        # Run the strategy on this ONE instrument (no carry term involved).
        r = fx_trend_following({t: data[t]}, **live_cfg)
        if r.empty:
            continue
        m = compute_metrics(r)
        if "error" in m:
            log.warning(f"  {t}: {m['error']}")
            continue
        rows.append({
            "ticker": t, "label": labels[t], "class": classes[t],
            "total_return": m["total_return"], "sharpe": m["sharpe"],
            "win_rate": m["win_rate"], "max_dd": m["max_drawdown"],
            "cagr": m["cagr"], "months": m["total_months"],
            "trades": int(r["trades_opened"].sum()),
        })

    rows.sort(key=lambda x: x["sharpe"], reverse=True)

    # ---- Per-instrument table (ranked by Sharpe) ----
    print(f"\n{'='*104}")
    print("FX-TREND STRATEGY ACROSS A BROADER UNIVERSE — LIVE CONFIG (SMA-200, MACD ON, RSI OFF)")
    print("Each instrument backtested in isolation, monthly rebalance, long-only, 3 bps cost. ANALYSIS ONLY.")
    if rows:
        print(f"Period: {start} to {data[fx_tickers[0]].index[-1].date()}  (ranked by Sharpe)")
    print(f"{'='*104}")
    print(f"{'Instrument':<16} {'Class':<9} {'TotRet%':>10} {'CAGR%':>8} {'Sharpe':>8} "
          f"{'WinRate%':>9} {'MaxDD%':>9} {'#Trades':>8} {'Months':>7}")
    print("-" * 104)
    for r in rows:
        print(f"{r['label']:<16} {r['class']:<9} {r['total_return']:>10} {r['cagr']:>8} "
              f"{r['sharpe']:>8} {r['win_rate']:>9} {r['max_dd']:>9} {r['trades']:>8} {r['months']:>7}")

    # ---- Per-class summary (mean of per-instrument metrics) ----
    by_class: dict[str, list] = {}
    for r in rows:
        by_class.setdefault(r["class"], []).append(r)

    def _mean(items, key):
        return round(sum(i[key] for i in items) / len(items), 3)

    class_summary = {}
    print(f"\n{'='*104}")
    print("PER-CLASS SUMMARY (mean of single-instrument backtests)")
    print(f"{'='*104}")
    print(f"{'Class':<10} {'#Instr':>7} {'Mean TotRet%':>13} {'Mean Sharpe':>12} "
          f"{'Mean WinRate%':>14} {'Mean MaxDD%':>12} {'Sum Trades':>11}")
    print("-" * 104)
    # order classes by mean Sharpe
    ordered = sorted(by_class.items(), key=lambda kv: _mean(kv[1], "sharpe"), reverse=True)
    for cls, items in ordered:
        summ = {
            "n": len(items),
            "mean_total_return": _mean(items, "total_return"),
            "mean_sharpe": _mean(items, "sharpe"),
            "mean_win_rate": _mean(items, "win_rate"),
            "mean_max_dd": _mean(items, "max_dd"),
            "sum_trades": sum(i["trades"] for i in items),
        }
        class_summary[cls] = summ
        print(f"{cls:<10} {summ['n']:>7} {summ['mean_total_return']:>13} "
              f"{summ['mean_sharpe']:>12} {summ['mean_win_rate']:>14} "
              f"{summ['mean_max_dd']:>12} {summ['sum_trades']:>11}")
    print(f"{'='*104}\n")

    return {"per_instrument": rows, "per_class": class_summary}


def compare_entry_filters(db_path: str | None = None, start: str = "2005-01-01") -> dict:
    """Backtest gate for the MACD + RSI entry-confirmation filters on the
    FX trend strategy. Compares:
        baseline  = SMA-200 only (filters OFF)
        variant_A = + MACD histogram agreement
        variant_B = + RSI exhaustion guard
        variant_C = + MACD + RSI (both)
    Prints a comparison table (total return, Sharpe, win rate, max DD, #trades)
    and returns the raw metrics dict per config.
    """
    init_db(db_path)
    log.info("Loading FX data for entry-filter comparison...")
    tickers = list(CURRENCY_PAIRS.keys())
    data = fetch_ohlcv(tickers, start=start)
    log.info(f"Loaded {len(data)} FX pairs")

    base = dict(FX_TREND_PARAMS)  # sma_period, top_n, cost_bps
    configs = {
        "baseline (SMA-200)": {**base, "macd_filter": False, "rsi_filter": False},
        "A: + MACD":          {**base, "macd_filter": True,  "rsi_filter": False},
        "B: + RSI":           {**base, "macd_filter": False, "rsi_filter": True},
        "C: + MACD + RSI":    {**base, "macd_filter": True,  "rsi_filter": True},
    }

    out = {}
    for name, cfg in configs.items():
        r = fx_trend_following(data, **cfg)
        if r.empty:
            log.warning(f"  {name}: no results")
            continue
        m = compute_metrics(r)
        n_trades = int(r["trades_opened"].sum()) if "trades_opened" in r.columns else None
        out[name] = {"metrics": m, "trades": n_trades,
                     "start": r.index[0].strftime("%Y-%m-%d"),
                     "end": r.index[-1].strftime("%Y-%m-%d")}

    print(f"\n{'='*92}")
    print("FX TREND ENTRY-FILTER BACKTEST (full sample, 10 pairs, monthly rebalance, long-only)")
    if out:
        any_row = next(iter(out.values()))
        print(f"Period: {any_row['start']} to {any_row['end']}")
    print(f"{'='*92}")
    print(f"{'Config':<22} {'TotRet%':>10} {'Sharpe':>8} {'WinRate%':>10} "
          f"{'MaxDD%':>9} {'#Trades':>9} {'Months':>8}")
    print("-" * 92)
    for name, r in out.items():
        m = r["metrics"]
        print(f"{name:<22} {m.get('total_return', 'N/A'):>10} {m.get('sharpe', 'N/A'):>8} "
              f"{m.get('win_rate', 'N/A'):>10} {m.get('max_drawdown', 'N/A'):>9} "
              f"{str(r['trades']):>9} {m.get('total_months', 'N/A'):>8}")
    print(f"{'='*92}\n")

    return out


def main():
    import sys
    if "--universe" in sys.argv:
        compare_universe()
    elif "--filters" in sys.argv:
        compare_entry_filters()
    else:
        compare_all()


if __name__ == "__main__":
    main()
