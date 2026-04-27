"""
Paper Executor Agent
--------------------
Manages paper trade lifecycle:
  - Opens trades approved by Risk Manager
  - Monitors open positions daily (stop loss, take profit)
  - Closes positions on exit signals or stop/TP hits
  - Tracks P&L and logs everything

Designed to run daily after Signal Generator + Risk Manager.

Usage:
    python3 -m pipeline.agents.paper_executor --monitor     # check stops/TPs on open positions
    python3 -m pipeline.agents.paper_executor --status      # show portfolio status
"""

import argparse
import json
import logging
import os
import sqlite3
from datetime import datetime

import pandas as pd

from pipeline.db import init_db, log_agent_action
from pipeline.agents.data_fetcher import fetch_ohlcv, SECTOR_ETFS, BENCHMARK

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

PORTFOLIO_VALUE = 100_000  # starting paper portfolio


def _get_broker():
    """Get Alpaca broker if API keys are set, else None (SQLite-only mode)."""
    if os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY"):
        from pipeline.agents.broker_alpaca import AlpacaBroker
        return AlpacaBroker()
    return None


def get_open_trades(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM paper_trades WHERE status = 'open' ORDER BY opened_at"
    ).fetchall()
    return [dict(r) for r in rows]


def get_latest_price(symbol: str) -> float | None:
    """Fetch latest close price for a symbol."""
    from datetime import timedelta
    start = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
    data = fetch_ohlcv([symbol], start=start, cache=False)
    if symbol in data and not data[symbol].empty:
        return float(data[symbol]["close"].iloc[-1])
    return None


def monitor_positions(conn: sqlite3.Connection, dry_run: bool = False) -> list[dict]:
    """
    Check all open positions against current prices.
    Close if stop loss or take profit hit.
    """
    open_trades = get_open_trades(conn)
    if not open_trades:
        log.info("No open positions to monitor.")
        return []

    log.info(f"Monitoring {len(open_trades)} open position(s)...")
    actions = []

    for trade in open_trades:
        symbol = trade["symbol"]
        current_price = get_latest_price(symbol)

        if current_price is None:
            log.warning(f"  {symbol}: could not get price, skipping")
            continue

        entry_price = trade["entry_price"]
        stop_loss = trade["stop_loss"]
        take_profit = trade["take_profit"]
        quantity = trade["quantity"]

        pnl = (current_price - entry_price) * quantity
        pnl_pct = (current_price - entry_price) / entry_price * 100
        r_multiple = (current_price - entry_price) / (entry_price - stop_loss) if entry_price != stop_loss else 0

        action = {
            "trade_id": trade["id"],
            "symbol": symbol,
            "entry": entry_price,
            "current": current_price,
            "stop": stop_loss,
            "tp": take_profit,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "r_multiple": round(r_multiple, 2),
            "action": "hold",
        }

        # Check stop loss
        if current_price <= stop_loss:
            action["action"] = "stop_loss"
            log.info(f"  {symbol}: STOP LOSS HIT @ ${current_price} (stop=${stop_loss})")

        # Check take profit
        elif current_price >= take_profit:
            action["action"] = "take_profit"
            log.info(f"  {symbol}: TAKE PROFIT HIT @ ${current_price} (tp=${take_profit})")

        else:
            log.info(f"  {symbol}: HOLD @ ${current_price} (P&L: ${pnl:.0f} / {pnl_pct:.1f}% / {r_multiple:.1f}R)")

        # Close if stop or TP hit
        if action["action"] in ("stop_loss", "take_profit") and not dry_run:
            close_trade(conn, trade, current_price, action["action"])

        actions.append(action)

    # Check for exit signals
    exit_signals = conn.execute(
        """SELECT s.* FROM signals s
           LEFT JOIN paper_trades pt ON pt.signal_id = s.id
           WHERE s.signal_type = 'exit'
           AND s.symbol IN (SELECT symbol FROM paper_trades WHERE status = 'open')
           AND pt.id IS NULL
           AND date(s.generated_at) = date('now')"""
    ).fetchall()

    for signal in exit_signals:
        signal = dict(signal)
        symbol = signal["symbol"]
        matching = [t for t in open_trades if t["symbol"] == symbol]
        for trade in matching:
            current_price = get_latest_price(symbol) or signal["price_at_signal"]
            if not dry_run:
                close_trade(conn, trade, current_price, "exit_signal")
            log.info(f"  {symbol}: EXIT SIGNAL — closing @ ${current_price}")
            actions.append({
                "trade_id": trade["id"],
                "symbol": symbol,
                "action": "exit_signal",
                "pnl": round((current_price - trade["entry_price"]) * trade["quantity"], 2),
            })

    log_agent_action(
        conn, "paper_executor", "monitor_completed",
        outputs={
            "positions_checked": len(open_trades),
            "stops_hit": sum(1 for a in actions if a["action"] == "stop_loss"),
            "tps_hit": sum(1 for a in actions if a["action"] == "take_profit"),
            "exits": sum(1 for a in actions if a["action"] == "exit_signal"),
        },
    )

    return actions


def close_trade(conn: sqlite3.Connection, trade: dict, exit_price: float, reason: str):
    """Close a paper trade and record P&L."""
    entry_price = trade["entry_price"]
    quantity = trade["quantity"]
    stop_loss = trade["stop_loss"]

    pnl = (exit_price - entry_price) * quantity
    r_multiple = (exit_price - entry_price) / (entry_price - stop_loss) if entry_price != stop_loss else 0

    conn.execute(
        """UPDATE paper_trades
           SET status = 'closed', exit_price = ?, pnl = ?, r_multiple = ?,
               closed_at = ?
           WHERE id = ?""",
        (
            round(exit_price, 2),
            round(pnl, 2),
            round(r_multiple, 2),
            datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            trade["id"],
        ),
    )
    conn.commit()

    log.info(f"  Closed [{trade['id']}] {trade['symbol']}: "
             f"P&L=${pnl:.0f} ({r_multiple:.1f}R) — {reason}")


def portfolio_status(conn: sqlite3.Connection) -> dict:
    """Generate portfolio status report."""
    open_trades = get_open_trades(conn)
    closed_trades = conn.execute(
        "SELECT * FROM paper_trades WHERE status = 'closed' ORDER BY closed_at DESC"
    ).fetchall()
    closed_trades = [dict(r) for r in closed_trades]

    # Realized P&L
    realized_pnl = sum(t["pnl"] or 0 for t in closed_trades)

    # Unrealized P&L
    unrealized_pnl = 0
    positions = []
    for trade in open_trades:
        current_price = get_latest_price(trade["symbol"])
        if current_price:
            upnl = (current_price - trade["entry_price"]) * trade["quantity"]
            unrealized_pnl += upnl
            positions.append({
                "symbol": trade["symbol"],
                "entry": trade["entry_price"],
                "current": current_price,
                "quantity": trade["quantity"],
                "pnl": round(upnl, 2),
                "stop": trade["stop_loss"],
                "tp": trade["take_profit"],
            })

    total_pnl = realized_pnl + unrealized_pnl
    portfolio_val = PORTFOLIO_VALUE + total_pnl

    # Win rate
    wins = sum(1 for t in closed_trades if (t["pnl"] or 0) > 0)
    total_closed = len(closed_trades)
    win_rate = (wins / total_closed * 100) if total_closed > 0 else 0

    # Avg R
    r_multiples = [t["r_multiple"] for t in closed_trades if t["r_multiple"] is not None]
    avg_r = sum(r_multiples) / len(r_multiples) if r_multiples else 0

    status = {
        "portfolio_value": round(portfolio_val, 2),
        "starting_value": PORTFOLIO_VALUE,
        "total_pnl": round(total_pnl, 2),
        "realized_pnl": round(realized_pnl, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "return_pct": round(total_pnl / PORTFOLIO_VALUE * 100, 2),
        "open_positions": len(open_trades),
        "closed_trades": total_closed,
        "win_rate": round(win_rate, 1),
        "avg_r_multiple": round(avg_r, 2),
        "positions": positions,
    }

    # Print
    print(f"\n{'='*70}")
    print(f"PAPER PORTFOLIO STATUS — {datetime.now().strftime('%Y-%m-%d')}")
    print(f"{'='*70}")
    print(f"  Portfolio Value:  ${portfolio_val:,.0f}")
    print(f"  Total P&L:       ${total_pnl:,.0f} ({status['return_pct']:+.1f}%)")
    print(f"  Realized:        ${realized_pnl:,.0f}")
    print(f"  Unrealized:      ${unrealized_pnl:,.0f}")
    print(f"  Win Rate:        {win_rate:.0f}% ({wins}/{total_closed})")
    print(f"  Avg R-Multiple:  {avg_r:.2f}")

    if positions:
        print(f"\n  OPEN POSITIONS ({len(positions)}):")
        print(f"  {'Symbol':>6} {'Entry':>8} {'Current':>8} {'P&L':>10} {'Stop':>8} {'TP':>8}")
        print(f"  {'─'*56}")
        for p in positions:
            print(f"  {p['symbol']:>6} ${p['entry']:>7.2f} ${p['current']:>7.2f} "
                  f"${p['pnl']:>9.0f} ${p['stop']:>7.2f} ${p['tp']:>7.2f}")

    if closed_trades:
        recent = closed_trades[:5]
        print(f"\n  RECENT CLOSED ({len(closed_trades)} total, showing last 5):")
        for t in recent:
            pnl = t["pnl"] or 0
            r = t["r_multiple"] or 0
            print(f"  {t['symbol']:>6} entry=${t['entry_price']:.2f} "
                  f"exit=${t['exit_price']:.2f} P&L=${pnl:+.0f} ({r:+.1f}R)")

    return status


# ---------------------------------------------------------------------------
# Daily pipeline runner
# ---------------------------------------------------------------------------

def execute_on_alpaca(conn: sqlite3.Connection, dry_run: bool = False):
    """
    Sync approved paper trades to Alpaca.
    Submits market orders for new entries, closes positions for exits.
    """
    broker = _get_broker()
    if not broker:
        log.info("  No Alpaca API keys — SQLite-only mode")
        return

    account = broker.get_account()
    log.info(f"  Alpaca account: ${account['equity']:,.0f} equity, "
             f"${account['buying_power']:,.0f} buying power")

    # Get open DB trades that haven't been sent to Alpaca yet
    unsent = conn.execute(
        "SELECT * FROM paper_trades WHERE status = 'open' AND broker_order_id IS NULL"
    ).fetchall()

    for trade in unsent:
        trade = dict(trade)
        if dry_run:
            log.info(f"  [DRY] Would buy {int(trade['quantity'])} {trade['symbol']}")
            continue

        try:
            order = broker.submit_order(
                symbol=trade["symbol"],
                qty=int(trade["quantity"]),
                side="buy",
                order_type="market",
            )
            conn.execute(
                "UPDATE paper_trades SET broker_order_id = ? WHERE id = ?",
                (order["order_id"], trade["id"]),
            )
            conn.commit()
            log.info(f"  Alpaca order submitted: {order['order_id']} "
                     f"({trade['symbol']} x{int(trade['quantity'])})")
        except Exception as e:
            log.error(f"  Failed to submit order for {trade['symbol']}: {e}")

    # Handle exits: close positions for closed DB trades
    recently_closed = conn.execute(
        """SELECT * FROM paper_trades
           WHERE status = 'closed' AND broker_order_id IS NOT NULL
           AND closed_at > datetime('now', '-1 day')"""
    ).fetchall()

    for trade in recently_closed:
        trade = dict(trade)
        pos = broker.get_position(trade["symbol"])
        if pos:
            if not dry_run:
                try:
                    broker.close_position(trade["symbol"])
                    log.info(f"  Alpaca position closed: {trade['symbol']}")
                except Exception as e:
                    log.error(f"  Failed to close {trade['symbol']}: {e}")
            else:
                log.info(f"  [DRY] Would close {trade['symbol']}")


def run_daily_pipeline(dry_run: bool = False, db_path: str | None = None):
    """
    Full daily pipeline:
    1. Generate signals
    2. Evaluate through risk manager
    3. Execute on Alpaca (if keys set)
    4. Monitor existing positions
    5. Print portfolio status
    """
    conn = init_db(db_path)

    broker = _get_broker()
    mode = "ALPACA PAPER" if broker else "SQLITE-ONLY"

    print(f"\n{'#'*70}")
    print(f"# DAILY PIPELINE — {datetime.now().strftime('%Y-%m-%d %H:%M')} [{mode}]")
    print(f"{'#'*70}")

    # Step 1: Generate signals
    from pipeline.agents.signal_generator import generate_signals
    print("\n[1/5] Generating signals...")
    signals = generate_signals(dry_run=dry_run, db_path=db_path)

    # Step 2: Risk management
    if signals and not dry_run:
        from pipeline.agents.risk_manager import process_signals
        print("\n[2/5] Evaluating signals through risk manager...")
        process_signals(pending=True, dry_run=dry_run, db_path=db_path)
    else:
        print("\n[2/5] No new signals to evaluate.")

    # Step 3: Execute on Alpaca
    print("\n[3/5] Executing on broker...")
    execute_on_alpaca(conn, dry_run=dry_run)

    # Step 4: Monitor existing positions
    print("\n[4/5] Monitoring open positions...")
    monitor_positions(conn, dry_run=dry_run)

    # Step 5: Portfolio status
    print("\n[5/5] Portfolio status...")
    portfolio_status(conn)

    log_agent_action(
        conn, "paper_executor", "daily_pipeline_completed",
        outputs={"signals": len(signals), "dry_run": dry_run, "mode": mode},
    )


def main():
    parser = argparse.ArgumentParser(description="Paper Executor — trade management")
    parser.add_argument("--monitor", action="store_true", help="Check stops/TPs on open positions")
    parser.add_argument("--status", action="store_true", help="Show portfolio status")
    parser.add_argument("--daily", action="store_true", help="Run full daily pipeline")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", type=str, default=None)
    args = parser.parse_args()

    if args.daily:
        run_daily_pipeline(dry_run=args.dry_run, db_path=args.db)
    elif args.monitor:
        conn = init_db(args.db)
        monitor_positions(conn, dry_run=args.dry_run)
    elif args.status:
        conn = init_db(args.db)
        portfolio_status(conn)
    else:
        # Default: show status
        conn = init_db(args.db)
        portfolio_status(conn)


if __name__ == "__main__":
    main()
