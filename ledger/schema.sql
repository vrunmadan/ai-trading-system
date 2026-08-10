-- Ledger schema. Every decision the system makes gets logged here,
-- whether or not it turns into a trade. This is what makes the Auditor
-- step meaningful — without it, the weekly review has nothing to check.

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    ticker TEXT NOT NULL,
    regime TEXT NOT NULL,              -- crash / bear / sideways / bull / euphoria
    strategy_bucket TEXT NOT NULL,     -- which strategy template fired
    direction TEXT NOT NULL,           -- BUY / SELL
    technical_score REAL,
    fundamental_score REAL,
    confidence_score REAL,             -- 0-100, researcher's own number
    researcher_rationale TEXT,
    qc_verdict TEXT,                   -- AGREE / DISAGREE / NEEDS_MORE_DATA
    qc_rationale TEXT,
    sized_quantity INTEGER,
    capital_to_deploy REAL,             -- INR approved by Risk Sizer (stored so Trader can reuse it)
    sizer_notes TEXT,
    alert_sent_at TEXT,
    user_response TEXT,                -- APPROVED / REJECTED / NO_RESPONSE
    status TEXT NOT NULL DEFAULT 'PENDING'  -- PENDING / EXECUTED / SKIPPED / MISSED
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER REFERENCES signals(id),
    ticker TEXT NOT NULL,
    direction TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    entry_price REAL,
    entry_time TEXT,
    exit_price REAL,
    exit_time TEXT,
    pnl REAL,
    mode TEXT NOT NULL DEFAULT 'PAPER'  -- PAPER / LIVE
);

CREATE TABLE IF NOT EXISTS position_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER REFERENCES trades(id),
    checked_at TEXT NOT NULL,
    price_at_check REAL,
    thesis_status TEXT,    -- INTACT / WEAKENING / INVALIDATED
    notes TEXT
);

CREATE TABLE IF NOT EXISTS weekly_audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start TEXT NOT NULL,
    week_end TEXT NOT NULL,
    summary TEXT,
    confidence_bucket_analysis TEXT,   -- did 80%+ confidence signals actually win 80%+?
    missed_opportunities TEXT,
    hypothesis_backlog TEXT,           -- candidate changes, NOT yet applied live
    model_used TEXT DEFAULT 'gemini-3.5-pro'
);

-- Portfolio peak tracking for drawdown circuit breaker.
-- Only ever has ONE row (upserted each cycle).
CREATE TABLE IF NOT EXISTS portfolio_peak (
    id INTEGER PRIMARY KEY CHECK (id = 1),  -- enforces single row
    peak_value REAL NOT NULL,
    updated_at TEXT NOT NULL
);
