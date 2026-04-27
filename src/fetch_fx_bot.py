"""Fetch trade data from the FX bot (local pipeline.db)."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def fetch_fx_stats(db_path: str | None = None) -> dict:
    """Pull FX trade stats from pipeline.db."""
    if db_path is None:
        db_path = str(Path(__file__).parent.parent / "data" / "pipeline.db")

    stats = {
        "source": "fx-bot",
        "broker": "Capital.com",
        "trades": [],
        "open_trades": [],
        "total_trades": 0,
        "today_trades": 0,
        "open_positions": 0,
        "win_count": 0,
        "loss_count": 0,
        "total_pnl": 0.0,
        "account_balance": 0.0,
        "last_run": None,
    }

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
    except Exception:
        return stats

    # Open trades
    try:
        rows = conn.execute(
            "SELECT * FROM paper_trades WHERE status = 'open' AND strategy_id IN (100, 101) ORDER BY opened_at DESC"
        ).fetchall()
        stats["open_trades"] = [dict(r) for r in rows]
        stats["open_positions"] = len(rows)
    except Exception:
        pass

    # Closed trades
    try:
        rows = conn.execute(
            "SELECT * FROM paper_trades WHERE status = 'closed' AND strategy_id IN (100, 101) ORDER BY closed_at DESC"
        ).fetchall()
        for r in rows:
            t = dict(r)
            stats["trades"].append(t)
            stats["total_trades"] += 1
            pnl = t.get("pnl") or 0
            stats["total_pnl"] += pnl
            if pnl > 0:
                stats["win_count"] += 1
            elif pnl < 0:
                stats["loss_count"] += 1
    except Exception:
        pass

    # Add open trades to total count
    stats["total_trades"] += stats["open_positions"]

    # Today's trades
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for t in stats["open_trades"] + stats["trades"]:
        opened = t.get("opened_at", "") or ""
        if opened.startswith(today):
            stats["today_trades"] += 1

    # Last pipeline run from agent_log
    try:
        row = conn.execute(
            "SELECT created_at FROM agent_log WHERE agent = 'fx_pipeline' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            stats["last_run"] = dict(row)["created_at"]
    except Exception:
        pass

    # Active strategies
    try:
        rows = conn.execute(
            "SELECT id, name FROM strategies WHERE id IN (100, 101)"
        ).fetchall()
        stats["strategies"] = [dict(r) for r in rows]
    except Exception:
        stats["strategies"] = []

    conn.close()
    return stats
