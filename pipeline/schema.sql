-- Trading Strategy Validation Pipeline — Core Schema
-- All tables are append-only unless noted. No UPDATEs on papers/strategies
-- once they enter paper trading (phase 3+).

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================================================
-- PHASE 1: Research
-- ============================================================

-- Raw papers discovered by Paper Hunter
CREATE TABLE IF NOT EXISTS papers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,              -- 'quantpedia', 'ssrn', 'arxiv', 'aqr'
    source_id       TEXT,                       -- external ID (SSRN number, arXiv ID, etc.)
    url             TEXT,
    title           TEXT NOT NULL,
    authors         TEXT,                       -- comma-separated
    abstract        TEXT,
    published_date  TEXT,                       -- ISO 8601
    asset_class     TEXT,                       -- 'equity', 'fx', 'crypto', 'multi'
    claimed_sharpe  REAL,
    claimed_cagr    REAL,
    claimed_win_rate REAL,
    holding_period  TEXT,                       -- 'intraday', 'daily', 'weekly', 'monthly'
    search_terms    TEXT,                       -- what query found this paper
    fetched_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(source, source_id)
);

-- Structured strategy specs extracted by Paper Reader
CREATE TABLE IF NOT EXISTS strategies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id        INTEGER REFERENCES papers(id),
    name            TEXT NOT NULL,              -- short human-readable name
    status          TEXT NOT NULL DEFAULT 'candidate',
        -- candidate -> backtest_pass | backtest_fail
        -- backtest_pass -> paper_trading | killed
        -- paper_trading -> live | killed
    entry_rule      TEXT NOT NULL,              -- exact rule, plain English + pseudocode
    exit_rule       TEXT NOT NULL,
    asset_universe  TEXT NOT NULL,              -- e.g. 'S&P 500 constituents'
    data_requirements TEXT,                     -- what data is needed (OHLCV, fundamentals, etc.)
    position_sizing TEXT,                       -- described sizing approach
    holding_period  TEXT,
    parameters      TEXT,                       -- JSON: {"lookback": 20, "threshold": 0.5, ...}
    claimed_sharpe  REAL,
    claimed_cagr    REAL,
    claimed_max_dd  REAL,
    test_period     TEXT,                       -- '2000-01-01 to 2020-12-31'
    caveats         TEXT,                       -- any caveats from the paper
    frozen          INTEGER NOT NULL DEFAULT 0, -- 1 = locked, no edits (set at paper trading start)
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    killed_at       TEXT,
    kill_reason     TEXT
);

-- Cataloger's ranking of candidate strategies
CREATE TABLE IF NOT EXISTS strategy_rankings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id     INTEGER NOT NULL REFERENCES strategies(id),
    rule_clarity    REAL NOT NULL,              -- 0-1: how precisely defined are the rules?
    data_access     REAL NOT NULL,              -- 0-1: can we get the data easily?
    claimed_sharpe_score REAL NOT NULL,         -- 0-1: normalized claimed Sharpe
    recency_score   REAL NOT NULL,              -- 0-1: how recently validated?
    composite_score REAL NOT NULL,              -- weighted average
    rank            INTEGER,
    notes           TEXT,
    ranked_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Deduplication tracking
CREATE TABLE IF NOT EXISTS strategy_duplicates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kept_strategy_id    INTEGER NOT NULL REFERENCES strategies(id),
    duplicate_strategy_id INTEGER NOT NULL REFERENCES strategies(id),
    similarity_reason   TEXT,
    detected_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- ============================================================
-- PHASE 2: Backtesting (tables created now, populated later)
-- ============================================================

CREATE TABLE IF NOT EXISTS backtest_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id     INTEGER NOT NULL REFERENCES strategies(id),
    run_type        TEXT NOT NULL,              -- 'in_sample', 'out_of_sample', 'stress', 'monte_carlo'
    parameters      TEXT,                       -- JSON: parameter set used for this run
    data_start      TEXT,
    data_end        TEXT,
    split_point     TEXT,                       -- where in/out-of-sample split occurs
    sharpe          REAL,
    cagr            REAL,
    max_drawdown    REAL,
    win_rate        REAL,
    total_trades    INTEGER,
    avg_r_multiple  REAL,
    transaction_cost_bps REAL,
    beat_spy        INTEGER,                   -- 0/1
    report          TEXT,                       -- JSON: full backtest report
    variant_number  INTEGER NOT NULL DEFAULT 1, -- anti-overfit: track how many variants tested
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Kill log for failed strategies (phase 2+)
CREATE TABLE IF NOT EXISTS kill_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id     INTEGER NOT NULL REFERENCES strategies(id),
    phase           TEXT NOT NULL,              -- 'backtest', 'paper_trading', 'live'
    criterion       TEXT NOT NULL,              -- which kill criterion triggered
    details         TEXT,                       -- explanation
    killed_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- ============================================================
-- PHASE 3: Paper Trading (tables created now, populated later)
-- ============================================================

CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id     INTEGER NOT NULL REFERENCES strategies(id),
    signal_type     TEXT NOT NULL,              -- 'entry' or 'exit'
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,              -- 'long' or 'short'
    price_at_signal REAL,
    full_state      TEXT,                       -- JSON: all indicator values at signal time
    generated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id     INTEGER NOT NULL REFERENCES strategies(id),
    signal_id       INTEGER REFERENCES signals(id),
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    entry_price     REAL,
    exit_price      REAL,
    quantity         REAL,
    stop_loss       REAL,
    take_profit     REAL,
    thesis          TEXT NOT NULL,              -- written BEFORE entry
    risk_pct        REAL,                       -- % of portfolio risked
    r_multiple      REAL,                       -- outcome in R
    pnl             REAL,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending -> open -> closed
    broker_order_id TEXT,
    risk_approved   INTEGER,                   -- 0/1: did risk manager approve?
    risk_veto_reason TEXT,
    opened_at       TEXT,
    closed_at       TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- ============================================================
-- PHASE 4: Performance
-- ============================================================

CREATE TABLE IF NOT EXISTS performance_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id     INTEGER NOT NULL REFERENCES strategies(id),
    period          TEXT NOT NULL,              -- 'daily', 'weekly', 'monthly'
    period_start    TEXT NOT NULL,
    period_end      TEXT NOT NULL,
    sharpe          REAL,
    sortino         REAL,
    win_rate        REAL,
    avg_r_multiple  REAL,
    max_drawdown    REAL,
    total_pnl       REAL,
    spy_return      REAL,                      -- benchmark comparison
    divergence_from_backtest REAL,             -- % difference from expected
    report          TEXT,                       -- JSON: full report
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- ============================================================
-- CROSS-CUTTING: Immutable agent audit log
-- ============================================================

CREATE TABLE IF NOT EXISTS agent_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent           TEXT NOT NULL,              -- 'paper_hunter', 'paper_reader', 'cataloger', etc.
    action          TEXT NOT NULL,              -- what the agent did
    inputs          TEXT,                       -- JSON
    outputs         TEXT,                       -- JSON
    reasoning       TEXT,                       -- why the agent made this decision
    strategy_id     INTEGER REFERENCES strategies(id),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
