"""
Ledger database helpers — SQLite, no credentials needed.

Every decision the pipeline makes gets written here before any money moves.
This is what makes the Auditor meaningful and gives you a paper trail if
anything goes wrong.
"""

import sqlite3
import os
from contextlib import contextmanager
from datetime import datetime

DB_PATH = os.getenv("LEDGER_DB_PATH", "ledger/trading_ledger.db")
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")


def init_db():
    """Create the database and tables from schema.sql. Safe to run multiple times."""
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        with open(SCHEMA_PATH) as f:
            conn.executescript(f.read())
        # Migration: add exchange column to signals if it doesn't exist yet
        # (for existing Railway deployments that ran before this column was added)
        try:
            conn.execute("ALTER TABLE signals ADD COLUMN exchange TEXT NOT NULL DEFAULT 'NSE'")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists — normal on fresh init
    print(f"Ledger initialized at {DB_PATH}")


@contextmanager
def get_db():
    """Context manager — commits on success, rolls back on exception."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # safe for concurrent reads
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def now_ist() -> str:
    """Current timestamp string for logging."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Signal logging
# ---------------------------------------------------------------------------

def log_signal(signal, sizing, qc_verdict=None) -> int:
    """Insert a signal record and return its row ID."""
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO signals (
                created_at, ticker, exchange, regime, strategy_bucket, direction,
                technical_score, fundamental_score, confidence_score,
                researcher_rationale, qc_verdict, qc_rationale,
                sized_quantity, capital_to_deploy, sizer_notes, status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                now_ist(),
                signal.ticker,
                getattr(signal, "exchange", "NSE"),  # backward-compat if old signal object
                signal.regime.value,
                signal.strategy_bucket,
                signal.direction,
                signal.technical_score,
                signal.fundamental_score,
                signal.confidence_score,
                signal.rationale,
                qc_verdict.verdict if qc_verdict else None,
                qc_verdict.rationale if qc_verdict else None,
                sizing.quantity,
                sizing.capital_to_deploy,
                sizing.notes,
                "PENDING",
            ),
        )
        return cur.lastrowid


def update_signal_alert_sent(signal_id: int):
    with get_db() as conn:
        conn.execute(
            "UPDATE signals SET alert_sent_at=?, status='PENDING' WHERE id=?",
            (now_ist(), signal_id),
        )


def update_signal_response(signal_id: int, response: str):
    """response: 'APPROVED' | 'REJECTED' | 'NO_RESPONSE'"""
    status = "EXECUTED" if response == "APPROVED" else "MISSED"
    with get_db() as conn:
        conn.execute(
            "UPDATE signals SET user_response=?, status=? WHERE id=?",
            (response, status, signal_id),
        )


def skip_signal(signal_id: int, reason: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE signals SET status='SKIPPED', sizer_notes=? WHERE id=?",
            (reason, signal_id),
        )


# ---------------------------------------------------------------------------
# Trade logging
# ---------------------------------------------------------------------------

def log_trade(signal_id: int, ticker: str, direction: str,
              quantity: int, entry_price: float, mode: str = "PAPER") -> int:
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO trades (signal_id, ticker, direction, quantity,
                                entry_price, entry_time, mode)
            VALUES (?,?,?,?,?,?,?)
            """,
            (signal_id, ticker, direction, quantity, entry_price, now_ist(), mode),
        )
        return cur.lastrowid


def close_trade(trade_id: int, exit_price: float):
    with get_db() as conn:
        row = conn.execute(
            "SELECT direction, quantity, entry_price FROM trades WHERE id=?",
            (trade_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"Trade {trade_id} not found")
        qty, entry = row["quantity"], row["entry_price"]
        pnl = (exit_price - entry) * qty if row["direction"] == "BUY" else (entry - exit_price) * qty
        conn.execute(
            "UPDATE trades SET exit_price=?, exit_time=?, pnl=? WHERE id=?",
            (exit_price, now_ist(), round(pnl, 2), trade_id),
        )


# ---------------------------------------------------------------------------
# Portfolio queries (used by Risk Sizer)
# ---------------------------------------------------------------------------

def get_open_positions() -> list[dict]:
    """Returns trades that have an entry but no exit yet."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE entry_price IS NOT NULL AND exit_price IS NULL"
        ).fetchall()
        return [dict(r) for r in rows]


def get_weekly_pnl() -> float:
    """Sum of closed trade P&L since last Monday."""
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(pnl), 0) as total
            FROM trades
            WHERE exit_time >= date('now', 'weekday 1', '-7 days')
              AND pnl IS NOT NULL
            """
        ).fetchone()
        return float(row["total"])


# ---------------------------------------------------------------------------
# Audit queries
# ---------------------------------------------------------------------------

def get_signals_for_week(week_start: str, week_end: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE created_at BETWEEN ? AND ?",
            (week_start, week_end + " 23:59:59"),
        ).fetchall()
        return [dict(r) for r in rows]


def get_trades_for_week(week_start: str, week_end: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE entry_time BETWEEN ? AND ?",
            (week_start, week_end + " 23:59:59"),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Kite token storage (avoids Railway env var restarts for daily token refresh)
# ---------------------------------------------------------------------------

def _ensure_kv_store():
    """Create the kv_store table if it doesn't exist (called lazily)."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kv_store (
                key     TEXT PRIMARY KEY,
                value   TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def save_kite_token(access_token: str) -> None:
    """
    Upsert the Kite access_token into the ledger DB.
    Called by auto_refresh_kite_token.py after each successful login.
    """
    _ensure_kv_store()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO kv_store (key, value, updated_at)
            VALUES ('kite_access_token', ?, ?)
            ON CONFLICT(key) DO UPDATE
                SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (access_token, now_ist()),
        )
        conn.commit()


def get_kite_token() -> str | None:
    """
    Returns the most recently saved Kite access_token from the ledger DB,
    or None if no token has been saved yet (first run, or DB wiped).
    """
    try:
        _ensure_kv_store()
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT value FROM kv_store WHERE key = 'kite_access_token'"
            ).fetchone()
            return row[0] if row else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Portfolio risk helpers (used by risk_manager/portfolio_risk.py)
# ---------------------------------------------------------------------------

def get_all_time_pnl() -> float:
    """Sum of P&L from every closed trade ever (paper + live)."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) AS total FROM trades WHERE pnl IS NOT NULL"
        ).fetchone()
        return float(row["total"])


def get_update_portfolio_peak(current_value: float) -> float:
    """
    Returns the all-time peak portfolio value.
    If current_value is a new high, updates the stored peak and returns it.
    """
    with get_db() as conn:
        row = conn.execute("SELECT peak_value FROM portfolio_peak WHERE id = 1").fetchone()
        peak = float(row["peak_value"]) if row else current_value
        if current_value >= peak:
            conn.execute(
                """
                INSERT INTO portfolio_peak (id, peak_value, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET peak_value=excluded.peak_value,
                                              updated_at=excluded.updated_at
                """,
                (current_value, now_ist()),
            )
            return current_value
        return peak
