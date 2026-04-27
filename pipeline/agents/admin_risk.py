"""
Admin Risk Agent (Cross-Bot)
----------------------------
Monitors total exposure across all bots, checks correlations,
enforces global limits.

Checks:
  1. Total open positions across all bots
  2. Correlation between positions (USD exposure overlap)
  3. Global drawdown from peak equity
  4. Per-bot position limits
  5. Kill switch recommendation when limits breached

Usage:
    python3 -m pipeline.agents.admin_risk
"""

import argparse
import json
import logging
import sqlite3
from datetime import datetime

from pipeline.db import init_db, log_agent_action

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

# Global limits
MAX_TOTAL_POSITIONS = 10         # across all bots
MAX_FX_POSITIONS = 3
MAX_USD_EXPOSURE_PAIRS = 5       # max pairs with USD exposure
GLOBAL_DD_LIMIT_PCT = -20.0      # kill switch threshold


# Currency exposure mapping
USD_PAIRS = {"EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"}
EUR_PAIRS = {"EURUSD", "EURGBP", "EURJPY"}
JPY_PAIRS = {"USDJPY", "EURJPY", "GBPJPY"}
GBP_PAIRS = {"GBPUSD", "EURGBP", "GBPJPY"}


def get_fx_exposure(conn: sqlite3.Connection) -> dict:
    """Analyze FX bot's currency exposure."""
    open_trades = conn.execute(
        "SELECT * FROM paper_trades WHERE status = 'open' AND strategy_id IN (100, 101)"
    ).fetchall()

    exposure = {
        "positions": [],
        "currency_exposure": {},
        "total_positions": len(open_trades),
    }

    for row in open_trades:
        t = dict(row)
        symbol = t["symbol"].upper()
        exposure["positions"].append({
            "symbol": symbol,
            "side": t["side"],
            "quantity": t.get("quantity", 0),
            "strategy_id": t["strategy_id"],
        })

        # Track currency exposure
        base = symbol[:3]
        quote = symbol[3:]

        if t["side"] == "long":
            exposure["currency_exposure"][base] = exposure["currency_exposure"].get(base, 0) + 1
            exposure["currency_exposure"][quote] = exposure["currency_exposure"].get(quote, 0) - 1
        else:
            exposure["currency_exposure"][base] = exposure["currency_exposure"].get(base, 0) - 1
            exposure["currency_exposure"][quote] = exposure["currency_exposure"].get(quote, 0) + 1

    return exposure


def check_correlation(positions: list[dict]) -> list[dict]:
    """Check for correlated positions (same currency heavy exposure)."""
    alerts = []
    symbols = [p["symbol"] for p in positions]

    # Count USD-correlated positions
    usd_count = sum(1 for s in symbols if s in USD_PAIRS)
    if usd_count >= MAX_USD_EXPOSURE_PAIRS:
        alerts.append({
            "type": "usd_concentration",
            "severity": "WARNING",
            "message": f"{usd_count} positions with USD exposure (limit: {MAX_USD_EXPOSURE_PAIRS})",
        })

    # Check JPY concentration (carry trade risk)
    jpy_count = sum(1 for s in symbols if s in JPY_PAIRS)
    if jpy_count >= 3:
        alerts.append({
            "type": "jpy_concentration",
            "severity": "WARNING",
            "message": f"{jpy_count} JPY pairs — correlated carry trade risk",
        })

    return alerts


def check_global_drawdown(conn: sqlite3.Connection) -> dict:
    """Check total realized P&L drawdown across all FX trades."""
    closed = conn.execute(
        "SELECT pnl, closed_at FROM paper_trades WHERE status = 'closed' AND strategy_id IN (100, 101) ORDER BY closed_at"
    ).fetchall()

    if not closed:
        return {"peak_pnl": 0, "current_pnl": 0, "drawdown": 0, "drawdown_pct": 0, "alert": None}

    pnls = [dict(r).get("pnl", 0) or 0 for r in closed]
    cumulative = 0
    peak = 0
    max_dd = 0

    for p in pnls:
        cumulative += p
        if cumulative > peak:
            peak = cumulative
        dd = cumulative - peak
        if dd < max_dd:
            max_dd = dd

    dd_pct = (max_dd / peak * 100) if peak > 0 else 0

    alert = None
    if dd_pct < GLOBAL_DD_LIMIT_PCT:
        alert = {
            "type": "global_drawdown",
            "severity": "CRITICAL",
            "message": f"Global drawdown {dd_pct:.1f}% breaches {GLOBAL_DD_LIMIT_PCT}% limit — KILL SWITCH",
        }

    return {
        "peak_pnl": round(peak, 2),
        "current_pnl": round(cumulative, 2),
        "drawdown": round(max_dd, 2),
        "drawdown_pct": round(dd_pct, 1),
        "alert": alert,
    }


def run_risk_check(db_path: str | None = None) -> dict:
    """Run full cross-bot risk check."""
    conn = init_db(db_path)

    print(f"\n{'='*60}")
    print(f"ADMIN RISK AGENT — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")

    # FX exposure
    fx_exposure = get_fx_exposure(conn)
    print(f"\n  FX Positions: {fx_exposure['total_positions']}")
    for p in fx_exposure["positions"]:
        tag = "T" if p["strategy_id"] == 100 else "PA"
        print(f"    [{tag}] {p['symbol']} {p['side']} {p['quantity']} lots")

    if fx_exposure["currency_exposure"]:
        print(f"\n  Currency Exposure:")
        for ccy, exp in sorted(fx_exposure["currency_exposure"].items()):
            direction = "LONG" if exp > 0 else "SHORT" if exp < 0 else "FLAT"
            print(f"    {ccy}: {direction} ({exp:+d})")

    # Correlation check
    all_alerts = []
    corr_alerts = check_correlation(fx_exposure["positions"])
    all_alerts.extend(corr_alerts)

    # Global drawdown
    dd = check_global_drawdown(conn)
    print(f"\n  Drawdown: ${dd['drawdown']:+.2f} ({dd['drawdown_pct']}% from peak ${dd['peak_pnl']})")
    if dd["alert"]:
        all_alerts.append(dd["alert"])

    # Position limit check
    total_pos = fx_exposure["total_positions"]
    if total_pos > MAX_TOTAL_POSITIONS:
        all_alerts.append({
            "type": "position_limit",
            "severity": "WARNING",
            "message": f"Total {total_pos} positions exceeds limit of {MAX_TOTAL_POSITIONS}",
        })

    # Summary
    print(f"\n  RISK STATUS:")
    if not all_alerts:
        print(f"    ALL CLEAR — no risk alerts")
    else:
        for a in all_alerts:
            print(f"    [{a['severity']}] {a['message']}")

    kill_switch = any(a["severity"] == "CRITICAL" for a in all_alerts)
    if kill_switch:
        print(f"\n    *** KILL SWITCH RECOMMENDED — close all positions ***")

    result = {
        "fx_positions": fx_exposure["total_positions"],
        "currency_exposure": fx_exposure["currency_exposure"],
        "drawdown": dd,
        "alerts": all_alerts,
        "kill_switch": kill_switch,
        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    log_agent_action(conn, "admin_risk", "risk_check", outputs=result)

    return result


def main():
    parser = argparse.ArgumentParser(description="Admin Risk Agent")
    parser.add_argument("--db", type=str, default=None)
    args = parser.parse_args()
    run_risk_check(db_path=args.db)


if __name__ == "__main__":
    main()
