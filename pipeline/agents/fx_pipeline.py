"""
FX Pipeline
-----------
Unified runner for the forex trading pipeline:
  1. Generate FX signals (daily trend + 4h price action)
  2. Risk management (position sizing + leverage)
  3. Execute on OANDA / cTrader (if keys set)
  4. Monitor open positions (stop loss, take profit)
  5. Portfolio status + Telegram alert

Supports two brokers:
  - OANDA (REST API, instant setup, preferred)
  - cTrader/Fusion Markets (protobuf, needs KYC approval)

Usage:
    python3 -m pipeline.agents.fx_pipeline --daily              # full pipeline
    python3 -m pipeline.agents.fx_pipeline --daily --dry-run    # no execution
    python3 -m pipeline.agents.fx_pipeline --status             # portfolio only
"""

import argparse
import json
import logging
import os
import sqlite3
from datetime import datetime

from pipeline.db import init_db, log_agent_action

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

# FX-specific risk parameters
ACCOUNT_BALANCE = float(os.environ.get("FX_ACCOUNT_BALANCE", "10000"))
LEVERAGE = float(os.environ.get("FX_LEVERAGE", "500"))
MAX_RISK_PER_TRADE = 0.02  # 2% = $2 on $100 account
MAX_POSITIONS = 3
STOP_LOSS_PIPS = 50  # 50 pip stop (adjustable)


def _get_broker():
    """Get broker: OANDA (preferred) > cTrader > None."""
    if os.environ.get("OANDA_API_KEY") and os.environ.get("OANDA_ACCOUNT_ID"):
        from pipeline.agents.broker_oanda import OandaBroker
        return OandaBroker()
    if os.environ.get("CTRADER_CLIENT_ID") and os.environ.get("CTRADER_ACCESS_TOKEN"):
        from pipeline.agents.broker_ctrader import CTraderBroker
        broker = CTraderBroker()
        broker.connect()
        return broker
    return None


def fx_risk_check(conn: sqlite3.Connection, signals: list[dict]) -> list[dict]:
    """
    Simple FX risk manager for small account.
    - Max 3 positions
    - 2% risk per trade
    - Calculate micro lot size based on stop loss
    """
    open_trades = conn.execute(
        "SELECT * FROM paper_trades WHERE status = 'open' AND strategy_id IN (100, 101) ORDER BY opened_at"
    ).fetchall()
    open_count = len(open_trades)
    open_symbols = [dict(t)["symbol"] for t in open_trades]

    decisions = []

    for signal in signals:
        if signal["signal_type"] == "exit":
            decisions.append({**signal, "approved": True, "action": "exit"})
            continue

        symbol = signal["symbol"]

        if open_count >= MAX_POSITIONS:
            decisions.append({**signal, "approved": False, "reason": f"max {MAX_POSITIONS} positions"})
            continue

        if symbol in open_symbols:
            decisions.append({**signal, "approved": False, "reason": f"already holding {symbol}"})
            continue

        # Position sizing: risk $2 (2% of $100) with 50 pip stop
        # Pip value for micro lot (0.01): ~$0.10 for most pairs
        pip_value = 0.10
        risk_amount = ACCOUNT_BALANCE * MAX_RISK_PER_TRADE
        micro_lots = risk_amount / (STOP_LOSS_PIPS * pip_value)
        volume = max(int(micro_lots), 1)  # at least 1 micro lot

        decisions.append({
            **signal,
            "approved": True,
            "action": "entry",
            "micro_lots": volume,
            "risk_amount": round(risk_amount, 2),
            "stop_pips": STOP_LOSS_PIPS,
            "risk_pct": MAX_RISK_PER_TRADE * 100,
        })
        open_count += 1
        open_symbols.append(symbol)

    return decisions


def execute_decisions(conn: sqlite3.Connection, decisions: list[dict], dry_run: bool = False):
    """Store decisions to DB and optionally execute on cTrader."""
    broker = _get_broker() if not dry_run else None

    for d in decisions:
        if not d["approved"]:
            log.info(f"  VETOED: {d['symbol']} — {d.get('reason', 'unknown')}")
            continue

        if d["action"] == "entry":
            log.info(f"  ENTRY: {d['symbol']} {d['micro_lots']} micro lots, "
                     f"risk=${d['risk_amount']} ({d['risk_pct']}%), stop={d['stop_pips']}pips")

            if not dry_run:
                # Store to DB
                conn.execute(
                    """INSERT INTO paper_trades
                       (strategy_id, signal_id, symbol, side, entry_price,
                        quantity, thesis, risk_pct, risk_approved, status, opened_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'open', ?)""",
                    (
                        d["strategy_id"], d.get("signal_id"),
                        d["symbol"], d["side"], d["price_at_signal"],
                        d["micro_lots"],
                        f"FX {d['strategy']}: {d['symbol']} @ {d['price_at_signal']}",
                        d["risk_pct"],
                        datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    ),
                )
                conn.commit()

                # Execute on broker
                if broker:
                    try:
                        from pipeline.agents.broker_oanda import OandaBroker
                        if isinstance(broker, OandaBroker):
                            # OANDA: units = micro_lots * 1000
                            units = d["micro_lots"] * 1000
                            result = broker.submit_order(
                                symbol=d["symbol"],
                                units=units,
                                side="buy",
                                stop_loss_pips=d["stop_pips"],
                                take_profit_pips=d["stop_pips"] * 3,  # 3:1 R/R
                            )
                            # Update DB with trade ID
                            if result.get("trade_id"):
                                conn.execute(
                                    "UPDATE paper_trades SET broker_order_id = ? WHERE symbol = ? AND status = 'open' AND broker_order_id IS NULL",
                                    (result["trade_id"], d["symbol"]),
                                )
                                conn.commit()
                        else:
                            # cTrader fallback
                            from pipeline.agents.broker_ctrader import MICRO_LOT
                            volume = d["micro_lots"] * MICRO_LOT
                            result = broker.submit_order(
                                symbol=d["symbol"],
                                volume=volume,
                                side="buy",
                                stop_loss_pips=d["stop_pips"],
                            )
                        log.info(f"    Broker order: {result}")
                    except Exception as e:
                        log.error(f"    Broker execution failed: {e}")

        elif d["action"] == "exit":
            log.info(f"  EXIT: {d['symbol']}")
            if not dry_run:
                # Close in DB
                conn.execute(
                    """UPDATE paper_trades SET status = 'closed', closed_at = ?
                       WHERE symbol = ? AND status = 'open'""",
                    (datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"), d["symbol"]),
                )
                conn.commit()

    if broker:
        broker.disconnect()


def fx_portfolio_status(conn: sqlite3.Connection):
    """Show FX portfolio status."""
    open_trades = conn.execute(
        "SELECT * FROM paper_trades WHERE status = 'open' AND strategy_id IN (100, 101)"
    ).fetchall()
    closed_trades = conn.execute(
        "SELECT * FROM paper_trades WHERE status = 'closed' AND strategy_id IN (100, 101)"
    ).fetchall()

    realized = sum(dict(t).get("pnl", 0) or 0 for t in closed_trades)
    wins = sum(1 for t in closed_trades if (dict(t).get("pnl", 0) or 0) > 0)
    total = len(closed_trades)
    win_rate = (wins / total * 100) if total > 0 else 0

    print(f"\n{'='*60}")
    print(f"FX PORTFOLIO — {datetime.now().strftime('%Y-%m-%d')}")
    print(f"{'='*60}")
    print(f"  Account:    ${ACCOUNT_BALANCE:.0f} (leverage {LEVERAGE:.0f}:1)")
    print(f"  Realized:   ${realized:+.2f}")
    print(f"  Win Rate:   {win_rate:.0f}% ({wins}/{total})")
    print(f"  Open:       {len(open_trades)} position(s)")

    if open_trades:
        print(f"\n  OPEN POSITIONS:")
        for t in open_trades:
            t = dict(t)
            print(f"    {t['symbol']:>8} {t['side']} {t.get('quantity', '?')} lots "
                  f"@ {t['entry_price']} — opened {t.get('opened_at', '?')[:10]}")


def run_daily(dry_run: bool = False, db_path: str | None = None):
    conn = init_db(db_path)

    has_oanda = bool(os.environ.get("OANDA_API_KEY"))
    has_ctrader = bool(os.environ.get("CTRADER_CLIENT_ID"))
    mode = "OANDA" if has_oanda else "cTrader" if has_ctrader else "SQLITE-ONLY"

    print(f"\n{'#'*60}")
    print(f"# FX PIPELINE — {datetime.now().strftime('%Y-%m-%d %H:%M')} [{mode}]")
    print(f"{'#'*60}")

    # Step 1: Generate signals
    from pipeline.agents.fx_signal_generator import generate_fx_signals
    print("\n[1/4] Generating FX signals...")
    signals = generate_fx_signals(dry_run=dry_run, db_path=db_path)

    # Step 2: Risk check
    print("\n[2/4] Risk management...")
    if signals:
        decisions = fx_risk_check(conn, signals)
        approved = [d for d in decisions if d["approved"]]
        vetoed = [d for d in decisions if not d["approved"]]
        print(f"  {len(approved)} approved, {len(vetoed)} vetoed")

        # Step 3: Execute
        print("\n[3/4] Executing...")
        execute_decisions(conn, decisions, dry_run=dry_run)
    else:
        print("  No signals to evaluate.")
        print("\n[3/4] Nothing to execute.")

    # Step 4: Status
    print("\n[4/4] Portfolio status...")
    fx_portfolio_status(conn)

    log_agent_action(
        conn, "fx_pipeline", "daily_completed",
        outputs={"signals": len(signals), "mode": mode, "dry_run": dry_run},
    )


def main():
    parser = argparse.ArgumentParser(description="FX Pipeline — daily forex trading")
    parser.add_argument("--daily", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", type=str, default=None)
    args = parser.parse_args()

    if args.daily:
        run_daily(dry_run=args.dry_run, db_path=args.db)
    elif args.status:
        conn = init_db(args.db)
        fx_portfolio_status(conn)
    else:
        conn = init_db(args.db)
        fx_portfolio_status(conn)


if __name__ == "__main__":
    main()
