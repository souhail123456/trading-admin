"""
Export FX Trades
----------------
Exports all FX trades from pipeline.db to shared/fx_trades.jsonl.

CLI: python src/export_fx_trades.py
"""

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

FX_DB_PATH = os.environ.get("FX_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "pipeline.db"))
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "shared", "fx_trades.jsonl")

STRATEGY_NAMES = {100: "trend", 101: "price_action"}


def main():
    if not os.path.exists(FX_DB_PATH):
        print(f"[export-fx] Skipping — {FX_DB_PATH} not found")
        return

    try:
        conn = sqlite3.connect(FX_DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM paper_trades WHERE strategy_id IN (100, 101) ORDER BY opened_at"
        ).fetchall()
    except Exception as e:
        print(f"[export-fx] Error reading DB: {e}")
        return

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)

    with open(OUTPUT_PATH, "w") as f:
        for r in rows:
            r = dict(r)
            record = {
                "id": r["id"],
                "strategy_id": r.get("strategy_id"),
                "strategy_name": STRATEGY_NAMES.get(r.get("strategy_id"), str(r.get("strategy_id"))),
                "symbol": r.get("symbol", ""),
                "side": r.get("side", ""),
                "entry_price": r.get("entry_price"),
                "exit_price": r.get("exit_price"),
                "quantity": r.get("quantity"),
                "pnl": r.get("pnl"),
                "status": r.get("status", ""),
                "broker_order_id": r.get("broker_order_id"),
                "opened_at": r.get("opened_at"),
                "closed_at": r.get("closed_at"),
                "created_at": r.get("created_at"),
            }
            f.write(json.dumps(record, default=str) + "\n")

    conn.close()
    print(f"[export-fx] Exported {len(rows)} trades to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
