"""
Backfill History
----------------
One-time script to populate shared/daily_history.jsonl from historical data.
Sources: Stock portfolio CSV (GitHub), FX trades (pipeline.db), Polymarket trades (GitHub).

CLI: python src/backfill_history.py
"""

import csv
import io
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

HISTORY_PATH = os.path.join(os.path.dirname(__file__), "..", "shared", "daily_history.jsonl")
FX_DB_PATH = os.environ.get("FX_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "pipeline.db"))
POLYMARKET_REPO = os.environ.get("POLYMARKET_REPO", "souhail123456/polymarket-bot")
STOCK_REPO = os.environ.get("STOCK_REPO", "souhail123456/trading-bot")

STRATEGY_NAMES = {100: "trend", 101: "price_action"}


def load_existing_trade_ids(path: str) -> set:
    """Load all trade_ids already in the history file."""
    ids = set()
    if not os.path.exists(path):
        return ids
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if "trade_id" in rec:
                    ids.add(rec["trade_id"])
            except json.JSONDecodeError:
                continue
    return ids


def backfill_stock(gh_token: str) -> list[dict]:
    """Fetch PORTFOLIO-HISTORY.csv from stock bot repo and create daily_snapshot events."""
    events = []
    try:
        from github import Github
    except ImportError:
        print("[stock-backfill] Skipping — PyGithub not installed")
        return events

    try:
        g = Github(gh_token)
        repo = g.get_repo(STOCK_REPO)
        content = repo.get_contents("memory/PORTFOLIO-HISTORY.csv")
        csv_text = content.decoded_content.decode("utf-8")
    except Exception as e:
        print(f"[stock-backfill] Skipping — could not fetch CSV: {e}")
        return events

    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        # Map whatever columns exist
        ts = row.get("date") or row.get("timestamp") or row.get("Date") or ""
        if ts and "T" not in ts:
            ts = ts + "T21:00:00Z"

        equity = None
        for key in ("portfolio_value", "equity", "total_equity", "value", "Equity"):
            if key in row and row[key]:
                try:
                    equity = float(row[key])
                except (ValueError, TypeError):
                    pass
                break

        cash = None
        for key in ("cash", "Cash", "buying_power"):
            if key in row and row[key]:
                try:
                    cash = float(row[key])
                except (ValueError, TypeError):
                    pass
                break

        ev = {
            "event": "daily_snapshot",
            "bot": "stock",
            "timestamp": ts,
            "equity": equity,
            "cash": cash,
            "positions": [],
            "total_unrealized_pnl": 0.0,
        }
        # Include any other numeric fields
        for k, v in row.items():
            if k not in ("date", "timestamp", "Date", "portfolio_value", "equity",
                         "total_equity", "value", "Equity", "cash", "Cash", "buying_power"):
                try:
                    ev[k] = float(v) if v else None
                except (ValueError, TypeError):
                    ev[k] = v if v else None

        events.append(ev)

    print(f"[stock-backfill] {len(events)} daily snapshots from CSV")
    return events


def backfill_fx(existing_ids: set) -> list[dict]:
    """Read all FX trades from pipeline.db."""
    events = []

    if not os.path.exists(FX_DB_PATH):
        print(f"[fx-backfill] Skipping — {FX_DB_PATH} not found")
        return events

    try:
        conn = sqlite3.connect(FX_DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM paper_trades WHERE strategy_id IN (100, 101) ORDER BY opened_at"
        ).fetchall()
    except Exception as e:
        print(f"[fx-backfill] Skipping — DB error: {e}")
        return events

    for r in rows:
        r = dict(r)
        trade_id = f"fx_{r['id']}"
        if trade_id in existing_ids:
            continue
        events.append({
            "event": "trade",
            "bot": "fx",
            "timestamp": r.get("closed_at") or r.get("opened_at") or "",
            "trade_id": trade_id,
            "symbol": r.get("symbol", ""),
            "side": r.get("side", ""),
            "entry_price": r.get("entry_price"),
            "exit_price": r.get("exit_price"),
            "quantity": r.get("quantity"),
            "pnl": r.get("pnl"),
            "status": r.get("status", ""),
            "strategy": STRATEGY_NAMES.get(r.get("strategy_id"), str(r.get("strategy_id"))),
            "opened_at": r.get("opened_at"),
            "closed_at": r.get("closed_at"),
        })

    conn.close()
    print(f"[fx-backfill] {len(events)} trade events")
    return events


def backfill_polymarket(gh_token: str, existing_ids: set) -> list[dict]:
    """Fetch trade logs from polymarket bot repo."""
    events = []
    try:
        from github import Github
    except ImportError:
        print("[polymarket-backfill] Skipping — PyGithub not installed")
        return events

    try:
        g = Github(gh_token)
        repo = g.get_repo(POLYMARKET_REPO)
    except Exception as e:
        print(f"[polymarket-backfill] Skipping — GitHub error: {e}")
        return events

    file_configs = [
        ("logs/trades.jsonl", "poly_ev"),
        ("logs/weather_trades.jsonl", "poly_weather"),
        ("logs/econ_trades.jsonl", "poly_econ"),
    ]

    for file_path, prefix in file_configs:
        try:
            content = repo.get_contents(file_path)
            lines = content.decoded_content.decode("utf-8").strip().split("\n")
        except Exception:
            continue

        for idx, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            try:
                trade = json.loads(line)
            except json.JSONDecodeError:
                continue

            trade_id = f"{prefix}_{idx}"
            if trade_id in existing_ids:
                continue

            ts = trade.get("timestamp") or trade.get("created_at") or trade.get("time") or ""
            events.append({
                "event": "trade",
                "bot": "polymarket",
                "timestamp": ts,
                "trade_id": trade_id,
                "symbol": trade.get("market") or trade.get("question") or trade.get("symbol", ""),
                "side": trade.get("side") or trade.get("outcome") or "",
                "entry_price": trade.get("entry_price") or trade.get("price") or trade.get("avg_price"),
                "exit_price": trade.get("exit_price") or trade.get("sell_price"),
                "quantity": trade.get("quantity") or trade.get("amount") or trade.get("size"),
                "pnl": trade.get("pnl") or trade.get("profit"),
                "status": trade.get("status", ""),
            })

    print(f"[polymarket-backfill] {len(events)} trade events")
    return events


def main():
    print("=== Backfill History ===")
    gh_token = os.environ.get("GH_TOKEN", "")

    existing_ids = load_existing_trade_ids(HISTORY_PATH)
    print(f"Existing trade IDs: {len(existing_ids)}")

    all_events = []

    # Stock Bot — historical snapshots
    if gh_token:
        try:
            all_events.extend(backfill_stock(gh_token))
        except Exception as e:
            print(f"[stock-backfill] FAILED: {e}")
    else:
        print("[stock-backfill] Skipping — GH_TOKEN not set")

    # FX Bot — all trades
    try:
        all_events.extend(backfill_fx(existing_ids))
    except Exception as e:
        print(f"[fx-backfill] FAILED: {e}")

    # Polymarket Bot — all trades
    if gh_token:
        try:
            all_events.extend(backfill_polymarket(gh_token, existing_ids))
        except Exception as e:
            print(f"[polymarket-backfill] FAILED: {e}")
    else:
        print("[polymarket-backfill] Skipping — GH_TOKEN not set")

    if not all_events:
        print("No events to write.")
        return

    # Sort by timestamp
    def sort_key(ev):
        ts = ev.get("timestamp", "")
        return ts if ts else "9999"

    all_events.sort(key=sort_key)

    # Write (append to preserve any existing data)
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    with open(HISTORY_PATH, "a") as f:
        for ev in all_events:
            f.write(json.dumps(ev, default=str) + "\n")

    print(f"Wrote {len(all_events)} events to {HISTORY_PATH}")


if __name__ == "__main__":
    main()
