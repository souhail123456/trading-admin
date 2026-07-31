"""Database initialization and helpers for the strategy pipeline."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "pipeline.db"


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Get a SQLite connection with WAL mode and foreign keys enabled."""
    db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Create all tables from schema.sql and return the connection."""
    conn = get_connection(db_path)
    schema = SCHEMA_PATH.read_text()
    conn.executescript(schema)
    _seed_fx_strategies(conn)
    _apply_pnl_corrections(conn)
    _fix_deal_id_orphans(conn)
    return conn


PNL_CORRECTIONS: dict[int, float] = {
    8: 6.90,  # GBPJPY trade — was 3314.84 (raw JPY, not USD)
}


def _apply_pnl_corrections(conn: sqlite3.Connection) -> None:
    """One-time PnL corrections for trades with known inflated values."""
    for trade_id, correct_pnl in PNL_CORRECTIONS.items():
        conn.execute(
            "UPDATE paper_trades SET pnl = ? WHERE id = ? AND pnl != ?",
            (correct_pnl, trade_id, correct_pnl),
        )
    conn.commit()


# Capital.com returns different deal IDs from the confirm endpoint vs the
# positions endpoint. This caused reconciliation to orphan the original
# trade and insert a duplicate with the "correct" deal ID. Fix: restore
# the original (has proper thesis/signal_id), give it the correct deal ID,
# delete the reconciled duplicate.
_DEAL_ID_ORPHAN_FIXES: list[tuple[int, int]] = [
    # (orphaned_id_to_restore, reconciled_duplicate_id_to_delete)
    (9, 12),    # USDCHF
    (13, 14),   # GBPJPY
    (15, 17),   # USDJPY
    (16, 18),   # EURGBP
    (19, 20),   # AUDUSD
]


def _fix_deal_id_orphans(conn: sqlite3.Connection) -> None:
    """One-time fix for deal ID mismatch orphans."""
    for orphan_id, dup_id in _DEAL_ID_ORPHAN_FIXES:
        dup = conn.execute(
            "SELECT broker_order_id FROM paper_trades WHERE id = ? AND status = 'open'",
            (dup_id,),
        ).fetchone()
        if not dup:
            continue
        correct_deal_id = dict(dup)["broker_order_id"]
        conn.execute(
            "UPDATE paper_trades SET status = 'open', broker_order_id = ? WHERE id = ?",
            (correct_deal_id, orphan_id),
        )
        conn.execute("DELETE FROM paper_trades WHERE id = ?", (dup_id,))
    conn.commit()



# ---------------------------------------------------------------------------
# Permanent kill list — strategies killed by backtest or live performance.
# This is the AUTHORITATIVE source of truth. DB status is enforced from here
# on every init_db() call, so kills survive cache resets, DB copies, and
# manual edits. To revive a strategy, remove it from this dict.
# ---------------------------------------------------------------------------
KILLED_STRATEGIES: dict[int, str] = {
    101: "Sharpe -1.08 in backtest — PA strategy permanently killed 2026-07",
}


def _seed_fx_strategies(conn: sqlite3.Connection) -> None:
    """Ensure FX strategy rows exist (IDs 100, 101) with validated parameters.

    Killed strategies (listed in KILLED_STRATEGIES) have their status forced
    to 'killed' on every call, and their parameters are NOT updated.
    """
    strategies = [
        {
            "id": 100,
            "name": "FX Trend Following",
            "entry_rule": "SMA-200 filter, rank by trend strength, top 3 pairs",
            "exit_rule": "Close when price crosses below SMA-200",
            "universe": "10 major FX pairs",
            "parameters": json.dumps({
                "sma_period": 200,
                "top_n": 3,
                "stop_loss_pips": 80,
                "take_profit_pips": None,
                "max_hold_days": None,
                "stop_loss_pct": None,
            }),
        },
        {
            "id": 101,
            "name": "FX Price Action",
            "entry_rule": "Candlestick patterns (engulfing, pin bar, hammer) + weekly trend filter",
            "exit_rule": "Exit on opposing pattern or bear score >= 2",
            "universe": "10 major FX pairs",
            "parameters": json.dumps({
                "min_bull_score": 2,
                "stop_loss_pips": 40,
                "take_profit_pips": None,
                "max_hold_days": 15,
                "stop_loss_pct": 0.03,
            }),
        },
    ]

    for s in strategies:
        sid = s["id"]
        is_killed = sid in KILLED_STRATEGIES

        existing = conn.execute(
            "SELECT id, status FROM strategies WHERE id = ?", (sid,)
        ).fetchone()

        if not existing:
            status = "killed" if is_killed else "paper_trading"
            conn.execute(
                """INSERT INTO strategies
                   (id, name, status, entry_rule, exit_rule, asset_universe, parameters,
                    killed_at, kill_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (sid, s["name"], status, s["entry_rule"], s["exit_rule"],
                 s["universe"], s["parameters"],
                 datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if is_killed else None,
                 KILLED_STRATEGIES.get(sid)),
            )
        else:
            current_status = dict(existing)["status"]

            if is_killed and current_status != "killed":
                # Enforce the kill — strategy is in KILLED_STRATEGIES but DB
                # doesn't reflect it (cache restored old state, manual edit, etc.)
                conn.execute(
                    """UPDATE strategies
                       SET status = 'killed',
                           killed_at = ?,
                           kill_reason = ?
                       WHERE id = ?""",
                    (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     KILLED_STRATEGIES[sid], sid),
                )
                # Record in kill_log (idempotent — only insert if not already there)
                has_log = conn.execute(
                    "SELECT 1 FROM kill_log WHERE strategy_id = ? LIMIT 1", (sid,)
                ).fetchone()
                if not has_log:
                    conn.execute(
                        """INSERT INTO kill_log
                           (strategy_id, phase, criterion, details)
                           VALUES (?, 'paper_trading', 'backtest_sharpe', ?)""",
                        (sid, KILLED_STRATEGIES[sid]),
                    )

            if not is_killed:
                # Only sync parameters for LIVE strategies — dead ones are frozen
                conn.execute(
                    "UPDATE strategies SET parameters = ? WHERE id = ?",
                    (s["parameters"], sid),
                )

    conn.commit()


def get_strategy_params(conn: sqlite3.Connection, strategy_id: int) -> dict:
    """Load strategy parameters from DB."""
    row = conn.execute("SELECT parameters FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
    if row and dict(row).get("parameters"):
        return json.loads(dict(row)["parameters"])
    return {}


def log_agent_action(
    conn: sqlite3.Connection,
    agent: str,
    action: str,
    inputs: dict | None = None,
    outputs: dict | None = None,
    reasoning: str | None = None,
    strategy_id: int | None = None,
) -> None:
    """Append an immutable entry to the agent audit log."""
    conn.execute(
        """INSERT INTO agent_log (agent, action, inputs, outputs, reasoning, strategy_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            agent,
            action,
            json.dumps(inputs) if inputs else None,
            json.dumps(outputs) if outputs else None,
            reasoning,
            strategy_id,
        ),
    )
    conn.commit()
