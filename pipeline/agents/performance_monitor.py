"""
Performance Monitor Agent
-------------------------
Tracks trade outcomes across all strategies, detects decay, compares
live performance vs backtest expectations.

Responsibilities:
  - Rolling Sharpe ratio (30-day window)
  - Strategy decay detection (Sharpe drops below threshold)
  - Win rate tracking
  - Slippage measurement (expected vs actual fill)
  - Live vs backtest divergence alerts
  - Feeds back to Trading Admin for kill/replace decisions

Usage:
    python3 -m pipeline.agents.performance_monitor
    python3 -m pipeline.agents.performance_monitor --strategy-id 100
"""

import argparse
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import numpy as np

from pipeline.db import init_db, log_agent_action

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

# Thresholds
MIN_SHARPE_THRESHOLD = 0.5       # flag if rolling Sharpe drops below
MIN_WIN_RATE = 0.40              # flag if win rate drops below 40%
MAX_DRAWDOWN_PCT = -15.0         # flag if drawdown exceeds 15%
BACKTEST_DIVERGENCE_PCT = 50     # flag if live Sharpe is 50%+ worse than backtest

# Backtest benchmarks (from fx_backtester results)
BACKTEST_BENCHMARKS = {
    100: {"name": "FX Trend-Following", "oos_sharpe": 2.51, "oos_win_rate": 77, "oos_max_dd": -3.1},
    101: {"name": "FX Price Action", "oos_sharpe": 1.02, "oos_win_rate": 66, "oos_max_dd": -5.0},
}


def compute_rolling_sharpe(returns: list[float], window: int = 30) -> float | None:
    """Compute annualized Sharpe ratio from daily returns."""
    if len(returns) < max(window, 5):
        return None
    r = np.array(returns[-window:])
    if r.std() == 0:
        return 0.0
    return float((r.mean() / r.std()) * np.sqrt(252))


def analyze_strategy(conn: sqlite3.Connection, strategy_id: int) -> dict:
    """Analyze a single strategy's live performance."""
    benchmark = BACKTEST_BENCHMARKS.get(strategy_id, {})

    closed = conn.execute(
        "SELECT * FROM paper_trades WHERE status = 'closed' AND strategy_id = ? ORDER BY closed_at",
        (strategy_id,),
    ).fetchall()
    open_trades = conn.execute(
        "SELECT * FROM paper_trades WHERE status = 'open' AND strategy_id = ?",
        (strategy_id,),
    ).fetchall()

    closed = [dict(r) for r in closed]
    open_trades = [dict(r) for r in open_trades]

    total = len(closed)
    if total == 0:
        return {
            "strategy_id": strategy_id,
            "name": benchmark.get("name", f"Strategy {strategy_id}"),
            "status": "no_data",
            "total_trades": 0,
            "open_positions": len(open_trades),
            "alerts": [],
        }

    # Basic stats
    pnls = [float(t.get("pnl", 0) or 0) for t in closed]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    total_pnl = sum(pnls)
    win_rate = (wins / total * 100) if total > 0 else 0

    # Equity curve for drawdown
    equity = [0.0]
    for p in pnls:
        equity.append(equity[-1] + p)
    peak = equity[0]
    max_dd = 0.0
    for e in equity:
        if e > peak:
            peak = e
        dd = ((e - peak) / peak * 100) if peak > 0 else 0
        if dd < max_dd:
            max_dd = dd

    # Rolling Sharpe (treat each trade's P&L as a daily return proxy)
    rolling_sharpe = compute_rolling_sharpe(pnls)

    # Alerts
    alerts = []

    if rolling_sharpe is not None and rolling_sharpe < MIN_SHARPE_THRESHOLD:
        alerts.append({
            "type": "sharpe_decay",
            "severity": "WARNING",
            "message": f"Rolling Sharpe {rolling_sharpe:.2f} < {MIN_SHARPE_THRESHOLD}",
        })

    if win_rate < MIN_WIN_RATE * 100:
        alerts.append({
            "type": "low_win_rate",
            "severity": "WARNING",
            "message": f"Win rate {win_rate:.0f}% < {MIN_WIN_RATE*100:.0f}%",
        })

    if max_dd < MAX_DRAWDOWN_PCT:
        alerts.append({
            "type": "drawdown_breach",
            "severity": "CRITICAL",
            "message": f"Max drawdown {max_dd:.1f}% exceeds {MAX_DRAWDOWN_PCT}%",
        })

    # Backtest divergence
    if benchmark and rolling_sharpe is not None:
        bt_sharpe = benchmark.get("oos_sharpe", 0)
        if bt_sharpe > 0:
            divergence = (1 - rolling_sharpe / bt_sharpe) * 100
            if divergence > BACKTEST_DIVERGENCE_PCT:
                alerts.append({
                    "type": "backtest_divergence",
                    "severity": "WARNING",
                    "message": f"Live Sharpe {rolling_sharpe:.2f} vs backtest {bt_sharpe:.2f} ({divergence:.0f}% worse)",
                })

    return {
        "strategy_id": strategy_id,
        "name": benchmark.get("name", f"Strategy {strategy_id}"),
        "status": "active",
        "total_trades": total,
        "open_positions": len(open_trades),
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 1),
        "total_pnl": round(total_pnl, 2),
        "max_drawdown": round(max_dd, 1),
        "rolling_sharpe": round(rolling_sharpe, 2) if rolling_sharpe is not None else None,
        "backtest_sharpe": benchmark.get("oos_sharpe"),
        "backtest_win_rate": benchmark.get("oos_win_rate"),
        "alerts": alerts,
    }


def run_monitor(strategy_id: int | None = None, db_path: str | None = None) -> list[dict]:
    """Run performance monitoring across strategies."""
    conn = init_db(db_path)

    strategy_ids = [strategy_id] if strategy_id else [100, 101]
    results = []

    print(f"\n{'='*70}")
    print(f"PERFORMANCE MONITOR — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*70}")

    for sid in strategy_ids:
        r = analyze_strategy(conn, sid)
        results.append(r)

        print(f"\n  [{sid}] {r['name']} — {r['status']}")
        if r["status"] == "no_data":
            print(f"    No closed trades yet. Open: {r['open_positions']}")
            continue

        print(f"    Trades: {r['total_trades']} | W/L: {r['wins']}/{r['losses']} ({r['win_rate']}%)")
        print(f"    P&L: ${r['total_pnl']:+.2f} | Max DD: {r['max_drawdown']}%")
        print(f"    Rolling Sharpe: {r['rolling_sharpe'] or 'N/A'} (backtest: {r['backtest_sharpe'] or 'N/A'})")
        print(f"    Open: {r['open_positions']} position(s)")

        if r["alerts"]:
            print(f"    ALERTS:")
            for a in r["alerts"]:
                print(f"      [{a['severity']}] {a['message']}")

    # Store snapshot
    for r in results:
        if r["status"] != "no_data":
            try:
                conn.execute(
                    """INSERT INTO performance_snapshots
                       (strategy_id, period, period_start, period_end,
                        sharpe, win_rate, max_drawdown, total_pnl, report)
                       VALUES (?, 'daily', ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        r["strategy_id"],
                        datetime.now().strftime("%Y-%m-%d"),
                        datetime.now().strftime("%Y-%m-%d"),
                        r["rolling_sharpe"],
                        r["win_rate"],
                        r["max_drawdown"],
                        r["total_pnl"],
                        json.dumps(r),
                    ),
                )
                conn.commit()
            except Exception as e:
                log.warning(f"Failed to store snapshot for {r['strategy_id']}: {e}")

    log_agent_action(
        conn, "performance_monitor", "daily_check",
        outputs={"strategies": len(results), "alerts": sum(len(r["alerts"]) for r in results)},
    )

    # Summary
    total_alerts = sum(len(r["alerts"]) for r in results)
    critical = sum(1 for r in results for a in r["alerts"] if a["severity"] == "CRITICAL")
    print(f"\n  Summary: {total_alerts} alert(s), {critical} critical")

    return results


def main():
    parser = argparse.ArgumentParser(description="Performance Monitor")
    parser.add_argument("--strategy-id", type=int, default=None)
    parser.add_argument("--db", type=str, default=None)
    args = parser.parse_args()
    run_monitor(strategy_id=args.strategy_id, db_path=args.db)


if __name__ == "__main__":
    main()
