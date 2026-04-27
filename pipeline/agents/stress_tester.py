"""
Stress Tester Agent
-------------------
Tests strategy robustness across:
  1. Market regimes (bull/bear, high/low volatility)
  2. Parameter sensitivity (±variations on each key param)
  3. Monte Carlo (shuffle trade order, 1000 iterations)

Usage:
    python -m pipeline.agents.stress_tester --all
    python -m pipeline.agents.stress_tester --strategy-id 5
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
from pipeline.agents.backtester import (
    momentum_rotational, currency_momentum, compute_metrics, select_strategy_impl
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

MAX_PARAM_VARIANTS = 20  # anti-overfit hard limit from spec


# ---------------------------------------------------------------------------
# 1. Regime Testing
# ---------------------------------------------------------------------------

def test_regimes(
    data: dict[str, pd.DataFrame],
    impl_fn,
    impl_params: dict,
    benchmark_ticker: str = BENCHMARK,
) -> dict:
    """
    Split data into market regimes and test strategy in each.
    Regimes defined by SPY (or first ticker for FX):
      - Bull: SPY 12-month return > 0
      - Bear: SPY 12-month return <= 0
      - High vol: SPY 60-day realized vol > median
      - Low vol: SPY 60-day realized vol <= median
    """
    # Run strategy on full data
    results, _, _ = impl_fn(data, **impl_params)
    if results.empty:
        return {"error": "no results"}

    # Get benchmark for regime classification
    if benchmark_ticker in data:
        bench = data[benchmark_ticker]["close"]
    else:
        # FX: use first ticker
        first = list(data.values())[0]
        bench = first["close"]

    bench_monthly = bench.resample("ME").last()
    bench_12m_ret = bench_monthly.pct_change(12)
    bench_daily_vol = bench.pct_change().rolling(60).std() * np.sqrt(252)
    bench_monthly_vol = bench_daily_vol.resample("ME").last()
    vol_median = bench_monthly_vol.median()

    # Align regime labels with results index
    regime_results = {}
    for regime_name, mask_fn in [
        ("bull", lambda idx: bench_12m_ret.reindex(idx).fillna(0) > 0),
        ("bear", lambda idx: bench_12m_ret.reindex(idx).fillna(0) <= 0),
        ("high_vol", lambda idx: bench_monthly_vol.reindex(idx).fillna(vol_median) > vol_median),
        ("low_vol", lambda idx: bench_monthly_vol.reindex(idx).fillna(vol_median) <= vol_median),
    ]:
        mask = mask_fn(results.index)
        subset = results[mask].copy()
        if len(subset) >= 12:
            subset["cumulative"] = (1 + subset["return"]).cumprod()
            subset["bench_cumulative"] = (1 + subset["benchmark"]).cumprod()
            metrics = compute_metrics(subset)
            regime_results[regime_name] = metrics
        else:
            regime_results[regime_name] = {"error": "insufficient data", "months": len(subset)}

    return regime_results


# ---------------------------------------------------------------------------
# 2. Parameter Sensitivity
# ---------------------------------------------------------------------------

def test_parameter_sensitivity(
    data: dict[str, pd.DataFrame],
    impl_fn,
    base_params: dict,
    strategy_name: str,
) -> dict:
    """
    Test variations of key parameters: ±10%, ±20% on each numeric param.
    Returns metrics for each variation.
    """
    variations = []
    variant_count = 0

    # Generate variations for each numeric parameter
    for key, value in base_params.items():
        if not isinstance(value, (int, float)):
            continue
        if key == "cost_bps":
            continue  # don't vary cost

        for pct in [-20, -10, 10, 20]:
            new_val = value * (1 + pct / 100)
            if isinstance(value, int):
                new_val = max(1, round(new_val))
                if new_val == value:
                    continue
            else:
                new_val = round(new_val, 2)

            variant_params = {**base_params, key: new_val}
            variations.append((f"{key}={new_val} ({pct:+d}%)", variant_params))
            variant_count += 1

            if variant_count >= MAX_PARAM_VARIANTS:
                log.warning(f"  Hit anti-overfit limit ({MAX_PARAM_VARIANTS} variants)")
                break
        if variant_count >= MAX_PARAM_VARIANTS:
            break

    # Run base
    base_results, _, _ = impl_fn(data, **base_params)
    if base_results.empty:
        return {"error": "no base results"}
    base_metrics = compute_metrics(base_results)

    sensitivity = {"base": base_metrics, "variations": {}}

    for label, params in variations:
        try:
            results, _, _ = impl_fn(data, **params)
            if results.empty:
                sensitivity["variations"][label] = {"error": "no results"}
                continue
            metrics = compute_metrics(results)
            sensitivity["variations"][label] = metrics
        except Exception as e:
            sensitivity["variations"][label] = {"error": str(e)}

    # Compute stability score: how much do results change?
    sharpes = [v.get("sharpe", 0) for v in sensitivity["variations"].values()
               if isinstance(v.get("sharpe"), (int, float))]
    if sharpes and base_metrics.get("sharpe"):
        sharpe_std = np.std(sharpes)
        sharpe_range = max(sharpes) - min(sharpes)
        sensitivity["stability"] = {
            "sharpe_std": round(float(sharpe_std), 3),
            "sharpe_range": round(float(sharpe_range), 3),
            "sharpe_min": round(float(min(sharpes)), 3),
            "sharpe_max": round(float(max(sharpes)), 3),
            "all_positive": all(s > 0 for s in sharpes),
            "variant_count": len(sharpes),
        }

    return sensitivity


# ---------------------------------------------------------------------------
# 3. Monte Carlo
# ---------------------------------------------------------------------------

def test_monte_carlo(
    data: dict[str, pd.DataFrame],
    impl_fn,
    impl_params: dict,
    n_simulations: int = 1000,
) -> dict:
    """
    Shuffle monthly returns and recompute metrics N times.
    Shows how much of the result is from trade ordering vs real edge.
    """
    results, _, _ = impl_fn(data, **impl_params)
    if results.empty:
        return {"error": "no results"}

    monthly_returns = results["return"].values
    original_metrics = compute_metrics(results)

    simulated_sharpes = []
    simulated_cagrs = []
    simulated_max_dds = []

    rng = np.random.default_rng(42)

    for _ in range(n_simulations):
        shuffled = rng.permutation(monthly_returns)
        sim_df = pd.DataFrame({
            "return": shuffled,
            "benchmark": 0.0,
        }, index=results.index[:len(shuffled)])
        sim_df["cumulative"] = (1 + sim_df["return"]).cumprod()
        sim_df["bench_cumulative"] = 1.0

        metrics = compute_metrics(sim_df)
        if "error" not in metrics:
            simulated_sharpes.append(metrics["sharpe"])
            simulated_cagrs.append(metrics["cagr"])
            simulated_max_dds.append(metrics["max_drawdown"])

    if not simulated_sharpes:
        return {"error": "all simulations failed"}

    # Where does the actual result fall in the distribution?
    actual_sharpe = original_metrics.get("sharpe", 0)
    sharpe_percentile = float(np.mean([s <= actual_sharpe for s in simulated_sharpes]) * 100)

    return {
        "original": original_metrics,
        "simulations": n_simulations,
        "sharpe_percentile": round(sharpe_percentile, 1),
        "sharpe_mean": round(float(np.mean(simulated_sharpes)), 3),
        "sharpe_std": round(float(np.std(simulated_sharpes)), 3),
        "sharpe_5th": round(float(np.percentile(simulated_sharpes, 5)), 3),
        "sharpe_95th": round(float(np.percentile(simulated_sharpes, 95)), 3),
        "cagr_mean": round(float(np.mean(simulated_cagrs)), 2),
        "cagr_5th": round(float(np.percentile(simulated_cagrs, 5)), 2),
        "cagr_95th": round(float(np.percentile(simulated_cagrs, 95)), 2),
        "max_dd_mean": round(float(np.mean(simulated_max_dds)), 2),
        "max_dd_worst_5pct": round(float(np.percentile(simulated_max_dds, 5)), 2),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def stress_test_strategy(
    strategy: dict,
    data: dict[str, pd.DataFrame],
    conn: sqlite3.Connection,
) -> dict:
    """Run all 3 stress tests on a single strategy."""
    impl_fn, impl_params = select_strategy_impl(strategy)
    name = strategy["name"]

    log.info(f"\n{'='*70}")
    log.info(f"STRESS TESTING: [{strategy['id']}] {name}")
    log.info(f"{'='*70}")

    # 1. Regime test
    log.info("\n1. REGIME TESTING")
    regimes = test_regimes(data, impl_fn, impl_params)
    for regime, metrics in regimes.items():
        if "error" in metrics:
            log.info(f"  {regime}: {metrics}")
        else:
            log.info(f"  {regime}: Sharpe={metrics['sharpe']}, CAGR={metrics['cagr']}%, MaxDD={metrics['max_drawdown']}%")

    # 2. Parameter sensitivity
    log.info("\n2. PARAMETER SENSITIVITY")
    sensitivity = test_parameter_sensitivity(data, impl_fn, impl_params, name)
    if "stability" in sensitivity:
        stab = sensitivity["stability"]
        log.info(f"  Base Sharpe: {sensitivity['base'].get('sharpe')}")
        log.info(f"  Sharpe range across variants: {stab['sharpe_min']} to {stab['sharpe_max']}")
        log.info(f"  Sharpe std: {stab['sharpe_std']}")
        log.info(f"  All variants positive Sharpe: {stab['all_positive']}")
        log.info(f"  Variants tested: {stab['variant_count']}")
    for label, metrics in sensitivity.get("variations", {}).items():
        if "error" not in metrics:
            log.info(f"    {label}: Sharpe={metrics['sharpe']}, CAGR={metrics['cagr']}%")

    # 3. Monte Carlo
    log.info("\n3. MONTE CARLO (1000 simulations)")
    mc = test_monte_carlo(data, impl_fn, impl_params)
    if "error" not in mc:
        log.info(f"  Actual Sharpe: {mc['original'].get('sharpe')}")
        log.info(f"  Sharpe percentile: {mc['sharpe_percentile']}th (higher = more likely real edge)")
        log.info(f"  Simulated Sharpe: mean={mc['sharpe_mean']}, 5th-95th={mc['sharpe_5th']} to {mc['sharpe_95th']}")
        log.info(f"  Simulated CAGR: mean={mc['cagr_mean']}%, 5th-95th={mc['cagr_5th']}% to {mc['cagr_95th']}%")
        log.info(f"  Worst 5% max drawdown: {mc['max_dd_worst_5pct']}%")

    # Store stress test as a backtest_run
    report = {
        "regimes": _sanitize(regimes),
        "sensitivity": _sanitize(sensitivity),
        "monte_carlo": _sanitize(mc),
    }

    conn.execute(
        """INSERT INTO backtest_runs
           (strategy_id, run_type, parameters, sharpe, cagr, max_drawdown,
            win_rate, report, variant_number)
           VALUES (?, 'stress', ?, ?, ?, ?, ?, ?, ?)""",
        (
            strategy["id"],
            json.dumps(_sanitize(impl_params)),
            sensitivity["base"].get("sharpe") if "base" in sensitivity else None,
            sensitivity["base"].get("cagr") if "base" in sensitivity else None,
            sensitivity["base"].get("max_drawdown") if "base" in sensitivity else None,
            sensitivity["base"].get("win_rate") if "base" in sensitivity else None,
            json.dumps(report),
            sensitivity.get("stability", {}).get("variant_count", 0),
        ),
    )
    conn.commit()

    log_agent_action(
        conn, "stress_tester", "stress_test_completed",
        inputs={"strategy_id": strategy["id"]},
        outputs={
            "regime_bear_sharpe": regimes.get("bear", {}).get("sharpe"),
            "param_all_positive": sensitivity.get("stability", {}).get("all_positive"),
            "mc_percentile": mc.get("sharpe_percentile"),
        },
        strategy_id=strategy["id"],
    )

    return report


def _sanitize(obj):
    """Convert numpy types to native Python for JSON serialization."""
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return obj


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def stress_test_all(strategy_id: int | None = None, db_path: str | None = None) -> list[dict]:
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
        report = stress_test_strategy(strat, data, conn)
        results.append({"strategy_id": strat["id"], "name": strat["name"], "report": report})

    # Final summary
    if results:
        print(f"\n{'='*70}")
        print("STRESS TEST SUMMARY")
        print(f"{'='*70}")
        for r in results:
            rpt = r["report"]
            regimes = rpt.get("regimes", {})
            stab = rpt.get("sensitivity", {}).get("stability", {})
            mc = rpt.get("monte_carlo", {})

            bear_sharpe = regimes.get("bear", {}).get("sharpe", "N/A")
            all_pos = stab.get("all_positive", "N/A")
            mc_pct = mc.get("sharpe_percentile", "N/A")

            print(f"\n  [{r['strategy_id']}] {r['name']}")
            print(f"    Bear market Sharpe: {bear_sharpe}")
            print(f"    All param variants positive: {all_pos}")
            print(f"    Monte Carlo percentile: {mc_pct}th")

            # Verdict
            flags = []
            if isinstance(bear_sharpe, (int, float)) and bear_sharpe < 0:
                flags.append("FAILS in bear markets")
            if all_pos is False:
                flags.append("Some param variants go negative")
            if isinstance(mc_pct, (int, float)) and mc_pct < 50:
                flags.append("Monte Carlo suggests luck, not edge")

            if flags:
                print(f"    ⚠ WARNINGS: {'; '.join(flags)}")
            else:
                print(f"    ✓ PASSED all stress tests")

    return results


def main():
    parser = argparse.ArgumentParser(description="Stress Tester — regime, sensitivity, Monte Carlo")
    parser.add_argument("--strategy-id", type=int, default=None)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--db", type=str, default=None)
    args = parser.parse_args()

    sid = args.strategy_id if not args.all else None
    stress_test_all(strategy_id=sid, db_path=args.db)


if __name__ == "__main__":
    main()
