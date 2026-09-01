-- Ledger schema. Every decision the system makes gets logged here,
-- whether or not it turns into a trade. This is what makes the Auditor
-- step meaningful — without it, the weekly review has nothing to check.

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    ticker TEXT NOT NULL,
    exchange TEXT NOT NULL DEFAULT 'NSE',  -- NSE or BSE
    regime TEXT NOT NULL,              -- crash / bear / sideways / bull / euphoria
    strategy_bucket TEXT NOT NULL,     -- which strategy template fired
    direction TEXT NOT NULL,           -- BUY / SELL
    technical_score REAL,
    fundamental_score REAL,
    confidence_score REAL,             -- 0-100, researcher's own number
    researcher_rationale TEXT,
    qc_verdict TEXT,                   -- AGREE / DISAGREE / NEEDS_MORE_DATA
    qc_rationale TEXT,
    price_at_signal REAL,              -- LTP at evaluation time, whatever the outcome.
                                        -- Without this a QC_BLOCKED/QC_ERROR signal has
                                        -- no baseline price and can never be graded —
                                        -- see signal_shadow_checks below.
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
    mode TEXT NOT NULL DEFAULT 'PAPER',  -- PAPER / LIVE
    exchange TEXT NOT NULL DEFAULT 'NSE',  -- NSE / BSE, needed to price the position
    -- Fill lifecycle. A row is PENDING from the moment the user approves until
    -- the EOD reconciler checks Kite for it:
    --   PENDING       intent recorded, fill not yet verified
    --   CONFIRMED     found in kite.positions()/holdings(); entry_price is the real average
    --   NOT_EXECUTED  not found at reconciliation; stops counting toward exposure
    fill_status TEXT NOT NULL DEFAULT 'CONFIRMED',
    fill_note TEXT
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

-- Per-stock evaluation log — every stock evaluated in every cycle, including PASSes.
-- This is what lets you see "why nothing fired today" without reading Railway logs.
CREATE TABLE IF NOT EXISTS cycle_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_at TEXT NOT NULL,             -- timestamp of the cycle run
    regime TEXT NOT NULL,               -- regime at time of evaluation
    regime_confidence REAL,             -- regime confidence %
    ticker TEXT NOT NULL,
    exchange TEXT NOT NULL DEFAULT 'NSE',
    strategy TEXT NOT NULL,             -- strategy name evaluated
    verdict TEXT NOT NULL,              -- TRADE / PASS / ERROR / NO_TOKEN / THIN_HISTORY
    technical_score REAL,               -- 0-100 (null if Claude call failed)
    fundamental_score REAL,             -- 0-100
    confidence_score REAL,              -- 0-100 (the number that must reach 75)
    rsi REAL,
    supertrend TEXT,
    volume_ratio REAL,
    pct_from_52wk_high REAL,
    bollinger_position REAL,
    above_sma50 INTEGER,                -- 1/0
    rationale TEXT                      -- Claude's rationale (or error message)
);

-- Shadow price checks — the only way to know whether a QC_BLOCKED / QC_ERROR
-- signal was actually a missed opportunity or a correctly-avoided loser.
-- Paper-only, no order ever placed: just "what would this position be worth
-- N days later" using price_at_signal as the hypothetical entry. Feeds the
-- weekly Auditor's missed-opportunity review with real numbers instead of
-- qualitative pattern-matching.
CREATE TABLE IF NOT EXISTS signal_shadow_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL REFERENCES signals(id),
    horizon_days INTEGER NOT NULL,      -- 1 / 3 / 5 calendar days after created_at
    checked_at TEXT NOT NULL,
    price_at_check REAL NOT NULL,
    return_pct REAL NOT NULL,           -- direction-adjusted vs price_at_signal
    notes TEXT,
    UNIQUE(signal_id, horizon_days)     -- each horizon is checked at most once
);

-- Portfolio peak tracking for drawdown circuit breaker.
-- Only ever has ONE row (upserted each cycle).
CREATE TABLE IF NOT EXISTS portfolio_peak (
    id INTEGER PRIMARY KEY CHECK (id = 1),  -- enforces single row
    peak_value REAL NOT NULL,
    updated_at TEXT NOT NULL
);
