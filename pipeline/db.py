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
    """Ensure FX strategy rows exist (IDs 100, 101) so FK constraints pass."""
    for sid, name, entry, exit_, universe in [
        (100, "FX Trend Following", "SMA-200 filter, rank by trend strength, top 3 pairs",
         "Close when price crosses below SMA-200", "10 major FX pairs"),
        (101, "FX Price Action", "Candlestick patterns (engulfing, pin bar, hammer) + weekly trend filter",
         "Exit on opposing pattern or bear score >= 2", "10 major FX pairs"),
    ]:
        existing = conn.execute("SELECT id FROM strategies WHERE id = ?", (sid,)).fetchone()
        if not existing:
            conn.execute(
                """INSERT INTO strategies (id, name, status, entry_rule, exit_rule, asset_universe)
                   VALUES (?, ?, 'paper_trading', ?, ?, ?)""",
                (sid, name, entry, exit_, universe),
            )
    conn.commit()


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
