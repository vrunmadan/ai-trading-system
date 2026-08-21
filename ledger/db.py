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

import pytz

DB_PATH = os.getenv("LEDGER_DB_PATH", "ledger/trading_ledger.db")
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")
IST = pytz.timezone("Asia/Kolkata")


def init_db():
    """Create the database and tables from schema.sql. Safe to run multiple times."""
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        with open(SCHEMA_PATH) as f:
            conn.executescript(f.read())
        # Idempotent migrations for existing Railway deployments
        for migration in [
            "ALTER TABLE signals ADD COLUMN exchange TEXT NOT NULL DEFAULT 'NSE'",
            "ALTER TABLE trades ADD COLUMN exchange TEXT NOT NULL DEFAULT 'NSE'",
            # Existing rows predate the reconciler and were only ever written by
            # execute_trade, which places the order itself — so they are already
            # confirmed. New PENDING rows are written explicitly by the approve path.
            "ALTER TABLE trades ADD COLUMN fill_status TEXT NOT NULL DEFAULT 'CONFIRMED'",
            "ALTER TABLE trades ADD COLUMN fill_note TEXT",
        ]:
            try:
                conn.execute(migration)
                conn.commit()
            except sqlite3.OperationalError:
                pass  # Column already exists — normal on fresh init

        # One-time status backfill (safe to re-run; both statements are no-ops
        # once applied). See the signals.status vocabulary note below.
        #
        # 1. Rows marked EXECUTED by the old approve path asserted a fill that
        #    was never checked. Any of them without a CONFIRMED trade row are
        #    demoted to APPROVED, which is what actually happened.
        # 2. MISSED collapsed REJECTED and NO_RESPONSE into one unread value.
        #    user_response kept the truth, so it can be recovered exactly.
        try:
            conn.execute(
                """
                UPDATE signals SET status='APPROVED'
                WHERE status='EXECUTED'
                  AND id NOT IN (
                      SELECT signal_id FROM trades
                      WHERE signal_id IS NOT NULL AND fill_status='CONFIRMED'
                  )
                """
            )
            conn.execute(
                "UPDATE signals SET status=user_response "
                "WHERE status='MISSED' AND user_response IN ('REJECTED','NO_RESPONSE')"
            )
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Fresh DB where one of the tables is not built yet.
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
    """Current timestamp string for logging, in IST."""
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Signal logging
# ---------------------------------------------------------------------------

def log_signal(signal, sizing, qc_verdict=None, status: str = "PENDING") -> int:
    """
    Insert a signal record and return its row ID.

    status defaults to PENDING (an alert is about to go out). Signals killed
    at the QC gate are logged too, with QC_BLOCKED or QC_ERROR, so a blocked
    candidate leaves a trace instead of vanishing — the daily summary counts
    them and the weekly Auditor can review them as missed opportunities.
    """
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
                status,
            ),
        )
        return cur.lastrowid


def update_signal_alert_sent(signal_id: int):
    with get_db() as conn:
        conn.execute(
            "UPDATE signals SET alert_sent_at=?, status='PENDING' WHERE id=?",
            (now_ist(), signal_id),
        )


# signals.status vocabulary. Each value means exactly one thing:
#
#   PENDING       alert sent, awaiting your response
#   APPROVED      you approved it; the fill has NOT been verified yet
#   EXECUTED      a fill was confirmed against Kite — the ONLY state that
#                 asserts the trade actually happened
#   NOT_EXECUTED  approved, but the order never reached the market
#   REJECTED      you rejected it
#   NO_RESPONSE   the EOD sweep found it unanswered
#   SKIPPED       dropped before the alert (see skip_signal)
#
# Approving used to write EXECUTED directly, which asserted a fill nobody had
# checked for, and REJECTED/NO_RESPONSE both collapsed into MISSED — a value
# nothing ever read, while the weekly Auditor searched status for the very
# names that were being discarded.
_RESPONSE_TO_STATUS = {
    "APPROVED": "APPROVED",
    "REJECTED": "REJECTED",
    "NO_RESPONSE": "NO_RESPONSE",
}


def update_signal_response(signal_id: int, response: str):
    """
    Record your answer to an alert.

    APPROVED means intent, not execution. Only the reconciler may promote a
    signal to EXECUTED, and only after finding the fill (see
    mark_signal_executed / mark_signal_not_executed).
    """
    status = _RESPONSE_TO_STATUS.get(response, response)
    with get_db() as conn:
        conn.execute(
            "UPDATE signals SET user_response=?, status=? WHERE id=?",
            (response, status, signal_id),
        )


def mark_signal_executed(signal_id: int) -> None:
    """A fill was confirmed. This is the only place EXECUTED is written."""
    with get_db() as conn:
        conn.execute(
            "UPDATE signals SET status='EXECUTED' WHERE id=?", (signal_id,)
        )


def mark_signal_not_executed(signal_id: int) -> None:
    """Approved, but the order never reached the market."""
    with get_db() as conn:
        conn.execute(
            "UPDATE signals SET status='NOT_EXECUTED' WHERE id=?", (signal_id,)
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
              quantity: int, entry_price: float, mode: str = "PAPER",
              exchange: str = "NSE", fill_status: str = "CONFIRMED",
              fill_note: str | None = None) -> int:
    """
    Insert a trade row.

    fill_status defaults to CONFIRMED because the only historical caller
    (trader.kite_client.execute_trade) places the order itself and therefore
    knows the fill happened. The approve path uses log_pending_trade instead.
    """
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO trades (signal_id, ticker, direction, quantity,
                                entry_price, entry_time, mode, exchange,
                                fill_status, fill_note)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (signal_id, ticker, direction, quantity, entry_price, now_ist(),
             mode, exchange, fill_status, fill_note),
        )
        return cur.lastrowid


def log_pending_trade(signal_id: int, ticker: str, exchange: str, direction: str,
                      quantity: int, expected_price: float,
                      mode: str = "PAPER") -> int:
    """
    Record the user's approved intent, before any fill is known.

    entry_price holds the *expected* price (approved capital / quantity). The
    reconciler replaces it with the real average price from Kite when it
    confirms the fill. Writing this row at approve time is what makes same-day
    exposure visible to the portfolio gate and the Risk Sizer — without it,
    several signals approved in one batch cannot see each other.
    """
    return log_trade(
        signal_id=signal_id, ticker=ticker, direction=direction,
        quantity=quantity, entry_price=expected_price, mode=mode,
        exchange=exchange, fill_status="PENDING",
        fill_note="Awaiting EOD reconciliation against Kite.",
    )


def get_trade_for_signal(signal_id: int) -> dict | None:
    """Returns the existing trade row for a signal, if any (double-approve guard)."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM trades WHERE signal_id=? ORDER BY id LIMIT 1",
            (signal_id,),
        ).fetchone()
        return dict(row) if row else None


def get_pending_trades() -> list[dict]:
    """Open trades whose fill has not yet been verified against Kite."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE fill_status='PENDING' AND exit_price IS NULL"
        ).fetchall()
        return [dict(r) for r in rows]


def confirm_trade(trade_id: int, fill_price: float, quantity: int | None = None,
                  note: str | None = None) -> None:
    """
    Promote a PENDING trade to CONFIRMED using the real fill from Kite.
    Overwrites entry_price (and quantity, on a partial fill) with the truth.
    """
    with get_db() as conn:
        if quantity is None:
            conn.execute(
                "UPDATE trades SET fill_status='CONFIRMED', entry_price=?, fill_note=? "
                "WHERE id=?",
                (fill_price, note, trade_id),
            )
        else:
            conn.execute(
                "UPDATE trades SET fill_status='CONFIRMED', entry_price=?, quantity=?, "
                "fill_note=? WHERE id=?",
                (fill_price, quantity, note, trade_id),
            )


def mark_trade_not_executed(trade_id: int, reason: str) -> None:
    """
    The approved order never reached the market. The row is kept for the audit
    trail but stops counting toward exposure (see get_open_positions).
    """
    with get_db() as conn:
        conn.execute(
            "UPDATE trades SET fill_status='NOT_EXECUTED', fill_note=? WHERE id=?",
            (reason, trade_id),
        )


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
    """
    Trades that have an entry and no exit yet, excluding orders the reconciler
    established were never placed.

    PENDING rows ARE included on purpose: an approved-but-unreconciled order is
    real exposure as far as the portfolio gate and the Risk Sizer are concerned,
    and treating it as such is what stops a batch of same-day approvals from
    each sizing as though the others did not exist.
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM trades "
            "WHERE entry_price IS NOT NULL AND exit_price IS NULL "
            "  AND fill_status != 'NOT_EXECUTED'"
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
# QC health — consecutive-error streak
#
# Persisted rather than held in memory so it survives the redeploys that reset
# every other in-process counter in this system.
# ---------------------------------------------------------------------------

_QC_STREAK_KEY = "qc_error_streak"


def get_qc_error_streak() -> int:
    """How many consecutive QC calls have failed. 0 when QC is healthy."""
    try:
        _ensure_kv_store()
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT value FROM kv_store WHERE key=?", (_QC_STREAK_KEY,)
            ).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def record_qc_error() -> int:
    """Increment the streak and return its new value."""
    streak = get_qc_error_streak() + 1
    try:
        _ensure_kv_store()
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """
                INSERT INTO kv_store (key, value, updated_at) VALUES (?,?,?)
                ON CONFLICT(key) DO UPDATE
                    SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (_QC_STREAK_KEY, str(streak), now_ist()),
            )
            conn.commit()
    except Exception as e:
        log_ = __import__("logging").getLogger(__name__)
        log_.error(f"Could not persist QC error streak: {e}")
    return streak


def reset_qc_error_streak() -> None:
    """Called after any QC call that produced a genuine verdict."""
    try:
        _ensure_kv_store()
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "DELETE FROM kv_store WHERE key=?", (_QC_STREAK_KEY,)
            )
            conn.commit()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Cycle evaluation log (every stock × strategy evaluated, including PASSes)
# ---------------------------------------------------------------------------

def log_cycle_evaluation(
    cycle_at: str,
    regime: str,
    regime_confidence: float,
    ticker: str,
    exchange: str,
    strategy: str,
    verdict: str,
    indicators: dict | None = None,
    technical_score: float | None = None,
    fundamental_score: float | None = None,
    confidence_score: float | None = None,
    rationale: str | None = None,
) -> None:
    """
    Log one stock × strategy evaluation to cycle_log.
    Called for every ticker in generate_signal(), including PASSes and errors.
    This is what lets you see why no signals fired.
    """
    ind = indicators or {}
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO cycle_log (
                cycle_at, regime, regime_confidence, ticker, exchange, strategy,
                verdict, technical_score, fundamental_score, confidence_score,
                rsi, supertrend, volume_ratio, pct_from_52wk_high, bollinger_position,
                above_sma50, rationale
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                cycle_at, regime, regime_confidence, ticker, exchange, strategy,
                verdict, technical_score, fundamental_score, confidence_score,
                ind.get("rsi_14"), ind.get("supertrend_10_3"), ind.get("volume_ratio_20d"),
                ind.get("pct_from_52wk_high"), ind.get("bollinger_position"),
                int(ind.get("above_sma50", False)), rationale,
            ),
        )


def get_cycle_log(days: int = 3) -> list[dict]:
    """Return all cycle_log rows from the last N days, newest first."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM cycle_log
            WHERE cycle_at >= datetime('now', ? || ' days')
            ORDER BY cycle_at DESC, ticker
            """,
            (f"-{days}",),
        ).fetchall()
        return [dict(r) for r in rows]


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
