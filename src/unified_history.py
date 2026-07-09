"""
Unified History Builder
-----------------------
Appends daily_snapshot and trade events to shared/daily_history.jsonl.
Pulls from all 3 bots: Stock (Alpaca), FX (pipeline.db), Polymarket (GitHub).

CLI: python src/unified_history.py
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

HISTORY_PATH = os.path.join(os.path.dirname(__file__), "..", "shared", "daily_history.jsonl")
FX_DB_PATH = os.environ.get("FX_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "pipeline.db"))
POLYMARKET_REPO = os.environ.get("POLYMARKET_REPO", "souhail123456/polymarket-bot")

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
                if rec.get("event") == "trade" and "trade_id" in rec:
                    ids.add(rec["trade_id"])
            except json.JSONDecodeError:
                continue
    return ids


def append_events(path: str, events: list[dict]):
    """Append events to the JSONL file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        for ev in events:
            f.write(json.dumps(ev, default=str) + "\n")


def collect_stock_events() -> list[dict]:
    """Collect snapshot and trade events from Alpaca (Stock Bot)."""
    events = []
    try:
        from pipeline.agents.broker_alpaca import AlpacaBroker
        broker = AlpacaBroker()
        account = broker.get_account()
        positions = broker.get_positions()
    except Exception as e:
        print(f"[stock] Skipping — Alpaca unavailable: {e}")
        return events

    now = datetime.now(timezone.utc).isoformat()

    pos_list = []
    total_unrealized = 0.0
    for p in positions:
        pos_list.append({
            "symbol": p["symbol"],
            "qty": p["qty"],
            "side": p["side"],
            "entry": p["avg_entry_price"],
            "current": p["current_price"],
            "unrealized_pnl": p["unrealized_pl"],
            "market_value": p["market_value"],
        })
        total_unrealized += p["unrealized_pl"]

    events.append({
        "event": "daily_snapshot",
        "bot": "stock",
        "timestamp": now,
        "equity": account["equity"],
        "cash": account["cash"],
        "positions": pos_list,
        "total_unrealized_pnl": round(total_unrealized, 2),
    })

    print(f"[stock] Snapshot: equity=${account['equity']:.2f}, {len(pos_list)} positions")
    return events


def collect_fx_events(existing_ids: set) -> list[dict]:
    """Collect snapshot and trade events from FX Bot (pipeline.db)."""
    events = []

    if not os.path.exists(FX_DB_PATH):
        print(f"[fx] Skipping — {FX_DB_PATH} not found")
        return events

    try:
        conn = sqlite3.connect(FX_DB_PATH)
        conn.row_factory = sqlite3.Row
    except Exception as e:
        print(f"[fx] Skipping — DB error: {e}")
        return events

    now = datetime.now(timezone.utc).isoformat()

    # Open positions for snapshot
    try:
        open_rows = conn.execute(
            "SELECT * FROM paper_trades WHERE status = 'open' AND strategy_id IN (100, 101)"
        ).fetchall()
    except Exception:
        open_rows = []

    # Total realized P&L
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) as total_pnl FROM paper_trades WHERE status = 'closed' AND strategy_id IN (100, 101)"
        ).fetchone()
        total_pnl = float(row["total_pnl"]) if row else 0.0
    except Exception:
        total_pnl = 0.0

    open_positions = []
    for r in open_rows:
        r = dict(r)
        open_positions.append({
            "symbol": r.get("symbol", ""),
            "side": r.get("side", ""),
            "entry": r.get("entry_price"),
            "quantity": r.get("quantity"),
        })

    events.append({
        "event": "daily_snapshot",
        "bot": "fx",
        "timestamp": now,
        "total_realized_pnl": round(total_pnl, 2),
        "open_positions": open_positions,
        "open_count": len(open_positions),
    })

    # All trades (open + closed) for trade events
    try:
        all_trades = conn.execute(
            "SELECT * FROM paper_trades WHERE strategy_id IN (100, 101) ORDER BY opened_at"
        ).fetchall()
    except Exception as e:
        print(f"[fx] Error reading trades: {e}")
        all_trades = []

    new_count = 0
    for r in all_trades:
        r = dict(r)
        trade_id = f"fx_{r['id']}"
        if trade_id in existing_ids:
            continue
        events.append({
            "event": "trade",
            "bot": "fx",
            "timestamp": r.get("closed_at") or r.get("opened_at") or now,
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
        new_count += 1

    conn.close()
    print(f"[fx] Snapshot: realized_pnl=${total_pnl:.2f}, {len(open_positions)} open. {new_count} new trade events.")
    return events


def collect_polymarket_events(existing_ids: set) -> list[dict]:
    """Collect trade events from Polymarket Bot via GitHub API."""
    gh_token = os.environ.get("GH_TOKEN")
    if not gh_token:
        print("[polymarket] Skipping — GH_TOKEN not set")
        return []

    try:
        from github import Github
    except ImportError:
        print("[polymarket] Skipping — PyGithub not installed")
        return []

    events = []
    try:
        g = Github(gh_token)
        repo = g.get_repo(POLYMARKET_REPO)
    except Exception as e:
        print(f"[polymarket] Skipping — GitHub error: {e}")
        return []

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

            # Normalize fields — polymarket trades have realized_pnl, won, size_usd, fee_usd
            ts = trade.get("timestamp") or trade.get("created_at") or trade.get("time") or ""
            resolved = trade.get("resolved", False)
            events.append({
                "event": "trade",
                "bot": "polymarket",
                "timestamp": ts,
                "trade_id": trade_id,
                "symbol": trade.get("market") or trade.get("question") or trade.get("symbol", ""),
                "side": trade.get("side") or trade.get("outcome") or "",
                "entry_price": trade.get("entry_price") or trade.get("price"),
                "exit_price": trade.get("exit_price") or trade.get("sell_price"),
                "size_usd": trade.get("size_usd") or trade.get("amount"),
                "pnl": trade.get("realized_pnl") if trade.get("realized_pnl") is not None else trade.get("pnl"),
                "won": trade.get("won"),
                "fee_usd": trade.get("fee_usd"),
                "status": "resolved" if resolved else trade.get("status", "open"),
                "strategy": trade.get("strategy", prefix.replace("poly_", "")),
                "category": trade.get("category"),
            })

    print(f"[polymarket] {len(events)} new trade events")
    return events


def main():
    print("=== Unified History Update ===")
    existing_ids = load_existing_trade_ids(HISTORY_PATH)
    print(f"Existing trade IDs in history: {len(existing_ids)}")

    all_events = []

    # Stock Bot
    try:
        all_events.extend(collect_stock_events())
    except Exception as e:
        print(f"[stock] FAILED: {e}")

    # FX Bot
    try:
        all_events.extend(collect_fx_events(existing_ids))
    except Exception as e:
        print(f"[fx] FAILED: {e}")

    # Polymarket Bot
    try:
        all_events.extend(collect_polymarket_events(existing_ids))
    except Exception as e:
        print(f"[polymarket] FAILED: {e}")

    if all_events:
        append_events(HISTORY_PATH, all_events)
        print(f"Appended {len(all_events)} events to {HISTORY_PATH}")
    else:
        print("No new events to append.")


if __name__ == "__main__":
    main()
