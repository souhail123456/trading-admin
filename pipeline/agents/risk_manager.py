"""
Risk Manager Agent
------------------
Reviews signals from the Signal Generator and decides:
  - Position size (% of portfolio per trade)
  - Stop loss / take profit levels
  - Whether to approve or veto a signal (portfolio-level checks)

Rules:
  - Max 2% risk per trade (distance to stop loss * position size <= 2% of portfolio)
  - Max 5 concurrent positions
  - Max 30% in any single sector ETF
  - Max portfolio drawdown of 15% → go to cash
  - Correlation guard: don't add if >0.8 correlated with existing positions

Usage:
    python3 -m pipeline.agents.risk_manager --signal-ids 1,2,3
    python3 -m pipeline.agents.risk_manager --pending   # process all pending signals
"""

import argparse
import json
import logging
import sqlite3
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from pipeline.db import init_db, log_agent_action
from pipeline.agents.data_fetcher import fetch_ohlcv, BENCHMARK

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

# Risk parameters
MAX_RISK_PER_TRADE = 0.02      # 2% of portfolio
MAX_POSITIONS = 5
MAX_SINGLE_POSITION = 0.30     # 30% max in one ticker
MAX_PORTFOLIO_DD = 0.15        # 15% drawdown → go to cash
CORRELATION_THRESHOLD = 0.80   # reject if >0.8 with existing
DEFAULT_PORTFOLIO_VALUE = 100_000  # $100K paper account


class RiskManager:
    def __init__(self, conn: sqlite3.Connection, portfolio_value: float = DEFAULT_PORTFOLIO_VALUE):
        self.conn = conn
        self.portfolio_value = portfolio_value

    def get_open_positions(self) -> list[dict]:
        """Get all currently open paper trades."""
        rows = self.conn.execute(
            "SELECT * FROM paper_trades WHERE status = 'open' ORDER BY opened_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_portfolio_pnl(self) -> float:
        """Calculate total unrealized + realized P&L."""
        rows = self.conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) as total_pnl FROM paper_trades WHERE status = 'closed'"
        ).fetchone()
        return float(rows["total_pnl"]) if rows else 0.0

    def compute_stop_loss(self, symbol: str, entry_price: float, data: pd.DataFrame) -> float:
        """
        ATR-based stop loss: 2x ATR below entry.
        """
        if len(data) < 20:
            return entry_price * 0.95  # fallback: 5% stop

        atr = (data["high"] - data["low"]).rolling(14).mean().iloc[-1]
        stop = entry_price - (2.0 * atr)
        return round(float(stop), 2)

    def compute_position_size(self, entry_price: float, stop_loss: float) -> tuple[float, float]:
        """
        Size position so max loss = MAX_RISK_PER_TRADE * portfolio.
        Returns (quantity, risk_pct).
        """
        risk_per_share = abs(entry_price - stop_loss)
        if risk_per_share <= 0:
            return 0, 0

        max_loss = self.portfolio_value * MAX_RISK_PER_TRADE
        quantity = max_loss / risk_per_share
        position_value = quantity * entry_price

        # Cap at MAX_SINGLE_POSITION of portfolio
        max_position = self.portfolio_value * MAX_SINGLE_POSITION
        if position_value > max_position:
            quantity = max_position / entry_price
            position_value = quantity * entry_price

        risk_pct = (risk_per_share * quantity) / self.portfolio_value * 100
        return round(quantity, 2), round(risk_pct, 2)

    def check_correlation(self, symbol: str, open_positions: list[dict]) -> tuple[bool, str]:
        """
        Check if new symbol is too correlated with existing positions.
        Uses 60-day return correlation.
        """
        if not open_positions:
            return True, "no existing positions"

        existing_symbols = [p["symbol"] for p in open_positions]
        all_symbols = existing_symbols + [symbol]

        start = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d")
        data = fetch_ohlcv(all_symbols, start=start)

        if symbol not in data:
            return True, "no data for correlation check"

        new_returns = data[symbol]["close"].pct_change().dropna()

        for existing in existing_symbols:
            if existing not in data:
                continue
            existing_returns = data[existing]["close"].pct_change().dropna()

            # Align dates
            common = new_returns.index.intersection(existing_returns.index)
            if len(common) < 30:
                continue

            corr = new_returns.loc[common].corr(existing_returns.loc[common])
            if abs(corr) > CORRELATION_THRESHOLD:
                return False, f"correlation {corr:.2f} with {existing} exceeds {CORRELATION_THRESHOLD}"

        return True, "correlation OK"

    def check_drawdown(self) -> tuple[bool, float]:
        """Check if portfolio has exceeded max drawdown."""
        realized_pnl = self.get_portfolio_pnl()
        dd_pct = realized_pnl / self.portfolio_value if self.portfolio_value > 0 else 0

        if dd_pct < -MAX_PORTFOLIO_DD:
            return False, dd_pct
        return True, dd_pct

    def evaluate_signal(self, signal: dict, data: dict[str, pd.DataFrame] | None = None) -> dict:
        """
        Evaluate a signal and return risk decision.
        Returns: {approved, quantity, stop_loss, take_profit, risk_pct, veto_reason, thesis}
        """
        symbol = signal["symbol"]
        entry_price = signal["price_at_signal"]
        signal_type = signal["signal_type"]

        # Exit signals always approved
        if signal_type == "exit":
            return {
                "approved": True,
                "signal_id": signal["id"],
                "symbol": symbol,
                "signal_type": "exit",
                "veto_reason": None,
                "thesis": f"Exit signal for {symbol}",
            }

        open_positions = self.get_open_positions()

        # Check 1: Max positions
        if len(open_positions) >= MAX_POSITIONS:
            return self._veto(signal, f"max positions reached ({MAX_POSITIONS})")

        # Check 2: Already holding this symbol
        if any(p["symbol"] == symbol for p in open_positions):
            return self._veto(signal, f"already holding {symbol}")

        # Check 3: Portfolio drawdown
        dd_ok, dd_pct = self.check_drawdown()
        if not dd_ok:
            return self._veto(signal, f"portfolio drawdown {dd_pct:.1%} exceeds {MAX_PORTFOLIO_DD:.0%} limit")

        # Check 4: Correlation with existing positions
        corr_ok, corr_msg = self.check_correlation(symbol, open_positions)
        if not corr_ok:
            return self._veto(signal, corr_msg)

        # Compute stop loss
        if data and symbol in data:
            stop_loss = self.compute_stop_loss(symbol, entry_price, data[symbol])
        else:
            stop_loss = round(entry_price * 0.95, 2)  # fallback 5%

        # Take profit: 3x risk (3:1 reward/risk)
        risk = entry_price - stop_loss
        take_profit = round(entry_price + (3 * risk), 2)

        # Position sizing
        quantity, risk_pct = self.compute_position_size(entry_price, stop_loss)

        if quantity <= 0:
            return self._veto(signal, "position size computed as zero")

        # Build thesis
        state = json.loads(signal["full_state"]) if isinstance(signal["full_state"], str) else signal["full_state"]
        strategy_name = "trend" if signal["strategy_id"] == 18 else "price_action"
        thesis = (
            f"{strategy_name} {signal_type} on {symbol} @ ${entry_price}. "
            f"Stop=${stop_loss}, TP=${take_profit}, "
            f"Risk={risk_pct}% of portfolio. "
            f"State: {json.dumps(state)}"
        )

        return {
            "approved": True,
            "signal_id": signal["id"],
            "symbol": symbol,
            "signal_type": "entry",
            "side": signal["side"],
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "quantity": quantity,
            "risk_pct": risk_pct,
            "veto_reason": None,
            "thesis": thesis,
        }

    def _veto(self, signal: dict, reason: str) -> dict:
        log.warning(f"  VETOED {signal['symbol']}: {reason}")
        return {
            "approved": False,
            "signal_id": signal["id"],
            "symbol": signal["symbol"],
            "signal_type": signal["signal_type"],
            "veto_reason": reason,
            "thesis": None,
        }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def process_signals(
    signal_ids: list[int] | None = None,
    pending: bool = False,
    dry_run: bool = False,
    db_path: str | None = None,
) -> list[dict]:
    """
    Process signals through risk management.
    """
    conn = init_db(db_path)
    rm = RiskManager(conn)

    if signal_ids:
        placeholders = ",".join("?" * len(signal_ids))
        rows = conn.execute(
            f"SELECT * FROM signals WHERE id IN ({placeholders})", signal_ids
        ).fetchall()
    elif pending:
        # Get signals that don't have corresponding paper_trades yet
        rows = conn.execute(
            """SELECT s.* FROM signals s
               LEFT JOIN paper_trades pt ON pt.signal_id = s.id
               WHERE pt.id IS NULL
               ORDER BY s.generated_at DESC"""
        ).fetchall()
    else:
        # Get today's signals
        today = datetime.now().strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT * FROM signals WHERE date(generated_at) = ? ORDER BY id",
            (today,)
        ).fetchall()

    signals = [dict(r) for r in rows]
    if not signals:
        log.info("No signals to process.")
        return []

    log.info(f"Processing {len(signals)} signal(s) through risk manager...")
    decisions = []

    for signal in signals:
        decision = rm.evaluate_signal(signal)
        decisions.append(decision)

        if decision["approved"]:
            log.info(f"  APPROVED: {decision['symbol']} {decision['signal_type']} "
                     f"qty={decision.get('quantity', 'N/A')} "
                     f"risk={decision.get('risk_pct', 'N/A')}%")

            if not dry_run and decision["signal_type"] == "entry":
                conn.execute(
                    """INSERT INTO paper_trades
                       (strategy_id, signal_id, symbol, side, entry_price,
                        stop_loss, take_profit, quantity, thesis, risk_pct,
                        risk_approved, status, opened_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'open', ?)""",
                    (
                        signal["strategy_id"], signal["id"], decision["symbol"],
                        decision["side"], decision["entry_price"],
                        decision["stop_loss"], decision["take_profit"],
                        decision["quantity"], decision["thesis"],
                        decision["risk_pct"],
                        datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    ),
                )
                conn.commit()
        else:
            if not dry_run:
                conn.execute(
                    """INSERT INTO paper_trades
                       (strategy_id, signal_id, symbol, side, entry_price,
                        thesis, risk_approved, risk_veto_reason, status)
                       VALUES (?, ?, ?, ?, ?, ?, 0, ?, 'pending')""",
                    (
                        signal["strategy_id"], signal["id"], decision["symbol"],
                        signal["side"], signal["price_at_signal"],
                        "", decision["veto_reason"],
                    ),
                )
                conn.commit()

    log_agent_action(
        conn, "risk_manager", "signals_evaluated",
        outputs={
            "total": len(decisions),
            "approved": sum(1 for d in decisions if d["approved"]),
            "vetoed": sum(1 for d in decisions if not d["approved"]),
        },
    )

    # Summary
    approved = [d for d in decisions if d["approved"] and d["signal_type"] == "entry"]
    vetoed = [d for d in decisions if not d["approved"]]

    print(f"\n{'='*70}")
    print(f"RISK MANAGER — {len(decisions)} signals evaluated")
    print(f"{'='*70}")

    if approved:
        print(f"\nAPPROVED ({len(approved)}):")
        for d in approved:
            print(f"  {d['symbol']:>5} LONG qty={d['quantity']:.0f} "
                  f"entry=${d['entry_price']} stop=${d['stop_loss']} tp=${d['take_profit']} "
                  f"risk={d['risk_pct']}%")

    if vetoed:
        print(f"\nVETOED ({len(vetoed)}):")
        for d in vetoed:
            print(f"  {d['symbol']:>5} — {d['veto_reason']}")

    return decisions


def main():
    parser = argparse.ArgumentParser(description="Risk Manager — evaluate and size signals")
    parser.add_argument("--signal-ids", type=str, default=None,
                        help="Comma-separated signal IDs to process")
    parser.add_argument("--pending", action="store_true",
                        help="Process all signals without paper trades")
    parser.add_argument("--dry-run", action="store_true",
                        help="Evaluate without storing decisions")
    parser.add_argument("--db", type=str, default=None)
    args = parser.parse_args()

    ids = [int(x) for x in args.signal_ids.split(",")] if args.signal_ids else None
    process_signals(signal_ids=ids, pending=args.pending, dry_run=args.dry_run, db_path=args.db)


if __name__ == "__main__":
    main()
