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
    return conn


def _seed_fx_strategies(conn: sqlite3.Connection) -> None:
    """Ensure FX strategy rows exist (IDs 100, 101) with validated parameters."""
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
        existing = conn.execute("SELECT id FROM strategies WHERE id = ?", (s["id"],)).fetchone()
        if not existing:
            conn.execute(
                """INSERT INTO strategies (id, name, status, entry_rule, exit_rule, asset_universe, parameters)
                   VALUES (?, ?, 'paper_trading', ?, ?, ?, ?)""",
                (s["id"], s["name"], s["entry_rule"], s["exit_rule"], s["universe"], s["parameters"]),
            )
        else:
            # Always sync parameters to ensure correct keys
            conn.execute("UPDATE strategies SET parameters = ? WHERE id = ?", (s["parameters"], s["id"]))
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
