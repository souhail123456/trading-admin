"""
Index Pipeline (stock-index trend) — Milestone 1: DRY-RUN ONLY
--------------------------------------------------------------
A lightweight, self-contained trend path for stock indices (S&P 500 + Nasdaq
100), kept deliberately SEPARATE from the FX universe scan/carry ranking. It
reuses the existing engine pieces:
  - data:   pipeline.agents.data_fetcher.fetch_ohlcv (yfinance)
  - signal: pipeline.agents.fx_signal_generator.macd_histogram (same MACD rule)
  - DB:     pipeline.db (signals + paper_trades tables)

Instruments (each invocation processes ALL of them in sequence):
  - S&P 500:   yfinance ^GSPC, Capital.com epic US500, strategy_id 200
  - Nasdaq 100: yfinance ^NDX,  Capital.com epic US100, strategy_id 201

Strategy (identical for every instrument — matches the winning TradingView
backtest, PF ~3.3, +136%, ~7% maxDD):
  - LONG ONLY (no shorts)
  - Entry:  close > SMA-200  AND  MACD histogram > 0
  - Exit:   "let it run" — close BELOW SMA-200 (SMA re-cross). No fixed TP.
  - Sizing: risk 2% of equity across a 2xATR(14) stop distance, in POINTS
            (index points, NOT FX pips). qty = risk_cash / stop_distance.

MILESTONE 1 = DRY-RUN: for each instrument this computes today's signal +
implied position and LOGS the intended action to the `signals` table (with the
instrument's strategy_id). If the signal would open or close, it also writes a
clearly-marked *intent* row into paper_trades (status='intent'). It NEVER calls
a broker and needs no secrets — it runs fully offline on public yfinance data.
Milestone 2 will add real demo order submission via Capital.com behind a GitHub
Actions step.

Usage:
    python3 -m pipeline.agents.index_pipeline --dry-run     # compute + log + print
    python3 -m pipeline.agents.index_pipeline --dry-run --db /path/to.db
"""

import argparse
import json
import logging
from datetime import datetime, timedelta

import pandas as pd

from pipeline.db import init_db, log_agent_action
from pipeline.agents.data_fetcher import fetch_ohlcv
from pipeline.agents.fx_signal_generator import macd_histogram

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

# Instruments — each isolated from FX (100 trend / 101 price-action) by its own
# strategy_id. All share the IDENTICAL config below. To add an index, append a
# row here; nothing else needs to change.
INSTRUMENTS = [
    {
        "strategy_id": 200,
        "name": "Index Trend (S&P 500)",
        "yf_ticker": "^GSPC",   # S&P 500 index daily OHLC on yfinance
        "epic": "US500",        # Capital.com epic (used in Milestone 2, not here)
        "symbol": "US500",      # symbol stored in DB (broker-facing name)
    },
    {
        "strategy_id": 201,
        "name": "Index Trend (Nasdaq 100)",
        "yf_ticker": "^NDX",    # Nasdaq 100 index daily OHLC on yfinance
        "epic": "US100",        # Capital.com epic (used in Milestone 2, not here)
        "symbol": "US100",      # symbol stored in DB (broker-facing name)
    },
]

# Strategy parameters (locked to the winning backtest)
SMA_PERIOD = 200
ATR_PERIOD = 14
ATR_STOP_MULT = 2.0        # 2x ATR stop distance
RISK_PCT = 0.02            # 2% of equity risked per trade
ACCOUNT_EQUITY = 10000.0   # dry-run notional equity for sizing display

# Point value assumption for the S&P index CFD (US500 on Capital.com):
# 1 unit = $1 P&L per 1.0 index point. Sizing below yields "units" = $/point
# exposure, exactly mirroring the Pine sizing qty = riskCash / stopDist.
POINT_VALUE_USD = 1.0


def atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    """Wilder's ATR(period) in price/point terms (matches Pine ta.atr)."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def compute_index_signal(df: pd.DataFrame, held_long: bool, instrument: dict) -> dict:
    """Compute today's long-only trend signal + intended action for one index.

    Args:
        df:         daily OHLC with columns open/high/low/close(/volume).
        held_long:  True if we currently hold a long (implied position).
        instrument: one entry from INSTRUMENTS (strategy_id/symbol/yf_ticker/...).

    Returns dict with the full state and an "action" in
    {"enter_long", "exit_long", "hold_long", "flat"}.
    """
    sma = df["close"].rolling(SMA_PERIOD).mean()
    hist = macd_histogram(df["close"])
    atr_series = atr(df)

    latest = df.iloc[-1]
    close = float(latest["close"])
    latest_sma = float(sma.iloc[-1])
    latest_hist = float(hist.iloc[-1])
    latest_atr = float(atr_series.iloc[-1])

    above_sma = close > latest_sma
    macd_bull = latest_hist > 0

    # Long-only decision, position-aware
    if held_long:
        # Exit only on SMA re-cross ("let it run", no fixed TP)
        action = "exit_long" if not above_sma else "hold_long"
    else:
        action = "enter_long" if (above_sma and macd_bull) else "flat"

    # Sizing (only meaningful for an entry): risk 2% across 2xATR stop, in points
    stop_distance = ATR_STOP_MULT * latest_atr
    risk_cash = ACCOUNT_EQUITY * RISK_PCT
    qty_units = (risk_cash / stop_distance) if stop_distance > 0 else 0.0
    stop_price = close - stop_distance  # long-only stop below entry

    return {
        "date": str(df.index[-1].date()),
        "strategy_id": instrument["strategy_id"],
        "symbol": instrument["symbol"],
        "epic": instrument["epic"],
        "yf_ticker": instrument["yf_ticker"],
        "close": round(close, 2),
        "sma_200": round(latest_sma, 2),
        "above_sma": bool(above_sma),
        "dist_to_sma_pct": round((close - latest_sma) / latest_sma * 100, 2) if latest_sma else None,
        "macd_histogram": round(latest_hist, 4),
        "macd_bullish": bool(macd_bull),
        "atr_14": round(latest_atr, 2),
        "stop_mult": ATR_STOP_MULT,
        "stop_distance_pts": round(stop_distance, 2),
        "stop_price": round(stop_price, 2),
        "risk_pct": RISK_PCT * 100,
        "risk_cash": round(risk_cash, 2),
        "account_equity": ACCOUNT_EQUITY,
        "point_value_usd": POINT_VALUE_USD,
        "qty_units": round(qty_units, 4),
        "held_long": bool(held_long),
        "action": action,
    }


def _current_position(conn, strategy_id: int) -> bool:
    """True if there is an OPEN long paper_trade for this strategy."""
    row = conn.execute(
        "SELECT 1 FROM paper_trades WHERE status = 'open' AND strategy_id = ? AND side = 'long' LIMIT 1",
        (strategy_id,),
    ).fetchone()
    return row is not None


def _ensure_strategy_row(conn, instrument: dict) -> None:
    """Seed the instrument's strategy row so signals/paper_trades FKs resolve.

    Additive and idempotent — does not touch the FX strategies (100/101).
    """
    sid = instrument["strategy_id"]
    exists = conn.execute("SELECT 1 FROM strategies WHERE id = ?", (sid,)).fetchone()
    if exists:
        return
    conn.execute(
        """INSERT INTO strategies
           (id, name, status, entry_rule, exit_rule, asset_universe,
            position_sizing, holding_period, parameters)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            sid,
            instrument["name"],
            "paper_trading",
            "LONG only: close > SMA-200 AND MACD histogram > 0",
            "Exit on SMA-200 re-cross (let it run, no fixed TP)",
            f"{instrument['symbol']} index (yfinance {instrument['yf_ticker']} / "
            f"Capital.com {instrument['epic']})",
            "Risk 2% of equity across 2xATR(14) stop, sized in index points",
            "Trend (multi-week/month)",
            json.dumps({
                "sma_period": SMA_PERIOD,
                "atr_period": ATR_PERIOD,
                "atr_stop_mult": ATR_STOP_MULT,
                "risk_pct": RISK_PCT,
                "long_only": True,
                "take_profit": None,
            }),
        ),
    )
    conn.commit()
    log.info(f"Seeded strategy row id={sid} ({instrument['name']})")


def _log_signal(conn, state: dict) -> int:
    """Write the intended action to the signals table. Returns the new row id."""
    action = state["action"]
    # signal_type reflects the intended action for easy eyeballing
    if action == "enter_long":
        signal_type, side = "entry", "long"
    elif action == "exit_long":
        signal_type, side = "exit", "long"
    elif action == "hold_long":
        signal_type, side = "hold", "long"
    else:  # flat
        signal_type, side = "flat", "long"

    cur = conn.execute(
        """INSERT INTO signals (strategy_id, signal_type, symbol, side, price_at_signal, full_state)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (state["strategy_id"], signal_type, state["symbol"], side,
         state["close"], json.dumps(state)),
    )
    conn.commit()
    return cur.lastrowid


def _log_intent_trade(conn, state: dict, signal_id: int) -> int:
    """Write a clearly-marked DRY-RUN intent row into paper_trades (NO broker).

    status='intent' keeps it out of every live path (monitor/reconcile filter on
    strategy_id IN (100,101) AND status='open'), so it can never fire an order.
    Only written when the signal would actually open or close a position.
    """
    action = state["action"]
    if action == "enter_long":
        thesis = (f"[DRY-RUN INTENT] Index trend ENTER LONG {state['symbol']} @ {state['close']} "
                  f"(SMA200={state['sma_200']}, MACD_hist={state['macd_histogram']}, "
                  f"stop={state['stop_price']}, qty={state['qty_units']})")
    else:  # exit_long
        thesis = (f"[DRY-RUN INTENT] Index trend EXIT LONG {state['symbol']} @ {state['close']} "
                  f"(SMA re-cross: close {state['close']} < SMA200 {state['sma_200']})")

    cur = conn.execute(
        """INSERT INTO paper_trades
           (strategy_id, signal_id, symbol, side, entry_price, quantity,
            stop_loss, thesis, risk_pct, risk_approved, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'intent')""",
        (
            state["strategy_id"], signal_id, state["symbol"], "long",
            state["close"], state["qty_units"], state["stop_price"],
            thesis, state["risk_pct"],
        ),
    )
    conn.commit()
    return cur.lastrowid


def run_instrument(conn, instrument: dict) -> dict:
    """Dry-run one index: fetch, compute, log signal (+ intent), print summary."""
    yf_ticker = instrument["yf_ticker"]
    sid = instrument["strategy_id"]
    _ensure_strategy_row(conn, instrument)

    # Need >= SMA_PERIOD + MACD/ATR warmup. Pull ~3 years of daily data.
    start = (datetime.now() - timedelta(days=1100)).strftime("%Y-%m-%d")
    log.info(f"Fetching {yf_ticker} daily OHLC from {start} ...")
    data = fetch_ohlcv([yf_ticker], start=start, cache=False)
    df = data.get(yf_ticker)
    if df is None or len(df) < SMA_PERIOD + ATR_PERIOD + 10:
        raise RuntimeError(
            f"Insufficient {yf_ticker} data (got {0 if df is None else len(df)} rows, "
            f"need >= {SMA_PERIOD + ATR_PERIOD + 10})"
        )
    log.info(f"Got {len(df)} daily bars ({df.index[0].date()} to {df.index[-1].date()})")

    held_long = _current_position(conn, sid)
    state = compute_index_signal(df, held_long=held_long, instrument=instrument)

    signal_id = _log_signal(conn, state)
    intent_id = None
    if state["action"] in ("enter_long", "exit_long"):
        intent_id = _log_intent_trade(conn, state, signal_id)

    log_agent_action(
        conn, "index_pipeline", "signal_generated",
        inputs={"ticker": yf_ticker, "held_long": held_long},
        outputs={"action": state["action"], "signal_id": signal_id, "intent_trade_id": intent_id},
        strategy_id=sid,
    )

    # ---- Human-readable summary ----
    print(f"\n{'=' * 70}")
    print(f"INDEX PIPELINE ({instrument['name']}) — DRY-RUN — {state['date']}")
    print(f"{'=' * 70}")
    print(f"  Instrument      : {state['symbol']}  (yfinance {yf_ticker}, broker epic {state['epic']})")
    print(f"  Close           : {state['close']}")
    print(f"  SMA-200         : {state['sma_200']}  "
          f"({'ABOVE' if state['above_sma'] else 'BELOW'}, {state['dist_to_sma_pct']:+}%)")
    print(f"  MACD histogram  : {state['macd_histogram']}  "
          f"({'BULLISH >0' if state['macd_bullish'] else 'bearish <=0'})")
    print(f"  ATR-14          : {state['atr_14']}  "
          f"(2xATR stop dist = {state['stop_distance_pts']} pts)")
    print(f"  Current position: {'LONG (held)' if held_long else 'FLAT'}")
    print(f"  -> SIGNAL       : {state['action'].upper()}")
    if state["action"] == "enter_long":
        print(f"     Intended entry: LONG {state['qty_units']} units @ {state['close']}  "
              f"(stop {state['stop_price']}, risk ${state['risk_cash']} = {state['risk_pct']}% equity)")
    elif state["action"] == "exit_long":
        print(f"     Intended exit : CLOSE LONG @ {state['close']} (SMA re-cross)")
    print(f"\n  Logged to signals table: id={signal_id} (strategy_id={sid})")
    if intent_id is not None:
        print(f"  Logged DRY-RUN intent to paper_trades: id={intent_id} (status='intent', NO broker order)")
    print(f"  [DRY-RUN] No broker order submitted.\n")

    return {"state": state, "signal_id": signal_id, "intent_trade_id": intent_id}


def run(dry_run: bool = True, db_path: str | None = None) -> list[dict]:
    if not dry_run:
        raise NotImplementedError(
            "Milestone 1 is DRY-RUN only. Live/demo order submission on the index "
            "epics arrives in Milestone 2 (Capital.com + GitHub Actions)."
        )

    conn = init_db(db_path)
    results = []
    for instrument in INSTRUMENTS:
        results.append(run_instrument(conn, instrument))
    return results


def main():
    parser = argparse.ArgumentParser(description="Index Pipeline (S&P 500 + Nasdaq 100 trend) — Milestone 1 dry-run")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute signal + log intended action (no broker). Required in Milestone 1.")
    parser.add_argument("--db", type=str, default=None)
    args = parser.parse_args()

    if not args.dry_run:
        parser.error("Milestone 1 supports --dry-run only (no live order submission yet).")
    run(dry_run=True, db_path=args.db)


if __name__ == "__main__":
    main()
