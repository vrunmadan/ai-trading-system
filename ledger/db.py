"""
Ledger database helpers — SQLite, no credentials needed.

Every decision the pipeline makes gets written here before any money moves.
This is what makes the Auditor meaningful and gives you a paper trail if
anything goes wrong.
"""

import sqlite3
import os
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytz

DB_PATH = os.getenv("LEDGER_DB_PATH", "ledger/trading_ledger.db")
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")
IST = pytz.timezone("Asia/Kolkata")


def current_mode() -> str:
    """
    PAPER or LIVE, read fresh from PAPER_MODE at call time so a config change
    takes effect without a restart. This is meant to be the single source of
    truth for which book is active — a previous incident (a confidence
    threshold hardcoded in one place while an env var controlled another)
    came from exactly this kind of duplicated, drifting config read.
    """
    return "PAPER" if os.getenv("PAPER_MODE", "true").lower() == "true" else "LIVE"


def _migrate_portfolio_peak_schema(conn) -> None:
    """
    portfolio_peak used to be a single global row (id INTEGER PK CHECK id=1)
    shared between PAPER and LIVE, so a live loss could offset a paper
    high-water mark and vice versa. Rebuilds the table in place, keyed by
    mode. The one existing peak is carried forward as PAPER (the only mode
    this system has run in so far); LIVE starts fresh the first time
    get_update_portfolio_peak(..., mode="LIVE") runs. Safe to call every
    startup: a no-op once the table already has a mode column, and a no-op
    on a brand-new DB where schema.sql already created the new shape.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(portfolio_peak)").fetchall()]
    if not cols or "mode" in cols:
        return
    old_row = conn.execute("SELECT peak_value, updated_at FROM portfolio_peak WHERE id=1").fetchone()
    conn.execute("ALTER TABLE portfolio_peak RENAME TO portfolio_peak_legacy")
    conn.execute(
        """
        CREATE TABLE portfolio_peak (
            mode TEXT PRIMARY KEY CHECK (mode IN ('PAPER', 'LIVE')),
            peak_value REAL NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    if old_row:
        conn.execute(
            "INSERT INTO portfolio_peak (mode, peak_value, updated_at) VALUES ('PAPER', ?, ?)",
            (old_row[0], old_row[1]),
        )
    conn.execute("DROP TABLE portfolio_peak_legacy")
    conn.commit()
    print("Migrated portfolio_peak to per-mode schema (existing peak kept as PAPER).")


def _dedupe_duplicate_live_trades(conn) -> None:
    """
    Enforcing "at most one live trade per signal" as a DB constraint (see
    idx_trades_one_live_per_signal below) fails outright if duplicates
    already exist — and the exact race that constraint closes could already
    have produced some, on a deployment that ran before this fix landed.

    For each signal_id with more than one non-NOT_EXECUTED trade row, keep
    the newest (highest id — the one the reconciler would have kept looking
    at via get_trade_for_signal's own newest-first ordering) and demote the
    rest to NOT_EXECUTED, the status this codebase already uses to mean
    "does not represent a live position" (see get_live_trade_for_signal).
    Nothing is deleted — the audit trail is kept, just reclassified — so
    this is safe to run on every startup.
    """
    dupes = conn.execute(
        """
        SELECT signal_id FROM trades
        WHERE signal_id IS NOT NULL AND fill_status != 'NOT_EXECUTED'
        GROUP BY signal_id HAVING COUNT(*) > 1
        """
    ).fetchall()
    for (signal_id,) in dupes:
        ids = [
            r[0] for r in conn.execute(
                "SELECT id FROM trades WHERE signal_id=? AND fill_status != 'NOT_EXECUTED' "
                "ORDER BY id DESC",
                (signal_id,),
            ).fetchall()
        ]
        for stale_id in ids[1:]:
            conn.execute(
                "UPDATE trades SET fill_status='NOT_EXECUTED', fill_note=? WHERE id=?",
                (
                    "Demoted: duplicate live trade row for the same signal, "
                    "found and closed by the 2026-09-19 review item-8 "
                    "uniqueness migration.",
                    stale_id,
                ),
            )
            print(f"Demoted duplicate trade #{stale_id} for signal #{signal_id} to NOT_EXECUTED.")
    if dupes:
        conn.commit()


def init_db():
    """Create the database and tables from schema.sql. Safe to run multiple times."""
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        # These two steps run BEFORE schema.sql below, not after, because
        # schema.sql now includes idx_trades_one_live_per_signal — a unique
        # index on trades.signal_id (2026-09-19 review, item 8). Creating
        # that index raises outright, aborting the rest of the script
        # (executescript stops at the first error), if the trades table
        # either lacks the fill_status column it filters on (a very old,
        # pre-fill_status deployment) or already contains the duplicate
        # live rows the index exists to prevent. Both are exactly the
        # states an existing Railway deployment might be in, so both must
        # be fixed up first. Neither step does anything on a brand-new DB —
        # there is no trades table yet, so each ALTER/SELECT below simply
        # raises "no such table", which is caught and skipped.
        for migration in [
            "ALTER TABLE signals ADD COLUMN exchange TEXT NOT NULL DEFAULT 'NSE'",
            "ALTER TABLE trades ADD COLUMN exchange TEXT NOT NULL DEFAULT 'NSE'",
            # Existing rows predate the reconciler and were only ever written by
            # execute_trade, which places the order itself — so they are already
            # confirmed. New PENDING rows are written explicitly by the approve path.
            "ALTER TABLE trades ADD COLUMN fill_status TEXT NOT NULL DEFAULT 'CONFIRMED'",
            "ALTER TABLE trades ADD COLUMN fill_note TEXT",
            "ALTER TABLE signals ADD COLUMN price_at_signal REAL",
            "ALTER TABLE trades ADD COLUMN baseline_quantity INTEGER NOT NULL DEFAULT 0",
        ]:
            try:
                conn.execute(migration)
                conn.commit()
            except sqlite3.OperationalError:
                pass  # Column already exists — normal on fresh init
        try:
            _dedupe_duplicate_live_trades(conn)
        except sqlite3.OperationalError:
            pass  # No trades table yet, or fill_status still missing — normal on fresh init

        with open(SCHEMA_PATH) as f:
            conn.executescript(f.read())

        # Re-run the same migrations: a DB that had no trades table at all
        # got it from schema.sql just above, with every column already
        # correct, so these are no-ops there — but a DB that already HAD a
        # trades table (the case the block above exists for) needs nothing
        # further here either, since schema.sql's CREATE TABLE IF NOT
        # EXISTS is a no-op for it. This second pass exists only so that
        # re-ordering the two blocks in the future can't silently drop a
        # migration; today it never does new work.
        for migration in [
            "ALTER TABLE signals ADD COLUMN exchange TEXT NOT NULL DEFAULT 'NSE'",
            "ALTER TABLE trades ADD COLUMN exchange TEXT NOT NULL DEFAULT 'NSE'",
            "ALTER TABLE trades ADD COLUMN fill_status TEXT NOT NULL DEFAULT 'CONFIRMED'",
            "ALTER TABLE trades ADD COLUMN fill_note TEXT",
            "ALTER TABLE signals ADD COLUMN price_at_signal REAL",
            "ALTER TABLE trades ADD COLUMN baseline_quantity INTEGER NOT NULL DEFAULT 0",
        ]:
            try:
                conn.execute(migration)
                conn.commit()
            except sqlite3.OperationalError:
                pass  # Column already exists

        _migrate_portfolio_peak_schema(conn)

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

def log_signal(
    signal, sizing, qc_verdict=None, status: str = "PENDING",
    price_at_signal: float | None = None,
) -> int:
    """
    Insert a signal record and return its row ID.

    status defaults to PENDING (an alert is about to go out). Signals killed
    at the QC gate are logged too, with QC_BLOCKED or QC_ERROR, so a blocked
    candidate leaves a trace instead of vanishing — the daily summary counts
    them and the weekly Auditor can review them as missed opportunities.
    PENDING rows later move to NO_RESPONSE if nothing is clicked before EOD.

    price_at_signal: the live LTP at the moment this signal was evaluated,
    when known (main.py fetches it for sizing before QC ever runs, so it's
    available regardless of what QC or the sizer decide). Without this, a
    signal that never became a trade — blocked, errored, or just never
    answered — had no recorded price and there was no way to later check
    whether that was actually the right outcome — see
    SHADOW_CHECK_STATUSES / get_signals_needing_shadow_check() /
    record_shadow_check().
    """
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO signals (
                created_at, ticker, exchange, regime, strategy_bucket, direction,
                technical_score, fundamental_score, confidence_score,
                researcher_rationale, qc_verdict, qc_rationale,
                sized_quantity, capital_to_deploy, sizer_notes, status,
                price_at_signal
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                price_at_signal,
            ),
        )
        signal_id = cur.lastrowid

    # Mirror EVERY signal — whatever its status (PENDING, QC_BLOCKED, QC_ERROR,
    # DROPPED_*, ...) — to the Google Sheets "Signals" tab, so the sheet reflects
    # each situation as it happens, not only the ones that alerted. Non-blocking.
    try:
        from sheets.trade_logger import append_signal
        append_signal(signal_id, signal, sizing, qc_verdict, status)
    except Exception:
        pass

    return signal_id


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


def count_signals_today(ticker: str, status: str) -> int:
    """How many signals with this ticker+status were logged today (IST).

    Used to throttle the immediate pre-QC drop alert to once per ticker+reason
    per day: a 75%+ candidate that keeps getting dropped every hourly cycle is
    worth one heads-up, not one email an hour.
    """
    import datetime
    import pytz

    d = datetime.datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d")
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM signals "
            "WHERE ticker=? AND status=? AND DATE(created_at)=?",
            (ticker, status, d),
        ).fetchone()
    return row["n"] if row else 0


# ---------------------------------------------------------------------------
# Trade logging
# ---------------------------------------------------------------------------

def log_trade(signal_id: int, ticker: str, direction: str,
              quantity: int, entry_price: float, mode: str = "PAPER",
              exchange: str = "NSE", fill_status: str = "CONFIRMED",
              fill_note: str | None = None, baseline_quantity: int = 0) -> int:
    """
    Insert a trade row.

    fill_status defaults to CONFIRMED because the only historical caller
    (trader.kite_client.execute_trade) places the order itself and therefore
    knows the fill happened. The approve path uses log_pending_trade instead.

    baseline_quantity: for a LIVE approval, how much of this ticker Kite
    already showed BEFORE this order — see log_pending_trade.
    """
    with get_db() as conn:
        entry_time = now_ist()
        cur = conn.execute(
            """
            INSERT INTO trades (signal_id, ticker, direction, quantity,
                                entry_price, entry_time, mode, exchange,
                                fill_status, fill_note, baseline_quantity)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (signal_id, ticker, direction, quantity, entry_price, entry_time,
             mode, exchange, fill_status, fill_note, baseline_quantity),
        )
        trade_id = cur.lastrowid

    # Mirror every opened trade (buy) to the Google Sheets "Trades" tab.
    # Best-effort and non-blocking — append_trade handles/logs its own errors.
    try:
        from sheets.trade_logger import append_trade
        append_trade(trade_id, signal_id, ticker, direction, quantity,
                     entry_price, entry_time, mode)
    except Exception:
        pass

    # Keep the "Positions" tab current — a new open position just changed it.
    try:
        from sheets.trade_logger import refresh_positions
        refresh_positions(get_open_positions())
    except Exception:
        pass

    return trade_id


def log_pending_trade(signal_id: int, ticker: str, exchange: str, direction: str,
                      quantity: int, expected_price: float,
                      mode: str = "PAPER", baseline_quantity: int = 0) -> int:
    """
    Record the user's approved intent, before any fill is known.

    entry_price holds the *expected* price (approved capital / quantity). The
    reconciler replaces it with the real average price from Kite when it
    confirms the fill. Writing this row at approve time is what makes same-day
    exposure visible to the portfolio gate and the Risk Sizer — without it,
    several signals approved in one batch cannot see each other.

    baseline_quantity: best-effort Kite snapshot of this ticker taken right
    before the order goes out (LIVE only). The reconciler only claims
    quantity above it, so an old personal holding of the same stock can never
    be confirmed as this order's fill (2026-09-19 review, item 3).
    """
    return log_trade(
        signal_id=signal_id, ticker=ticker, direction=direction,
        quantity=quantity, entry_price=expected_price, mode=mode,
        exchange=exchange, fill_status="PENDING",
        fill_note="Awaiting EOD reconciliation against Kite.",
        baseline_quantity=baseline_quantity,
    )


def get_trade_for_signal(signal_id: int) -> dict | None:
    """
    The CURRENT trade row for a signal, if any.

    Ordered newest-first. It previously ordered oldest-first, which meant any
    caller asking "what happened to this signal" got the first attempt rather
    than the live one.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM trades WHERE signal_id=? ORDER BY id DESC LIMIT 1",
            (signal_id,),
        ).fetchone()
        return dict(row) if row else None


def get_live_trade_for_signal(signal_id: int) -> dict | None:
    """
    The trade that already acts on this signal, if one exists.

    This is the double-approve guard's question, asked directly rather than
    inferred from whichever row happened to sort first. NOT_EXECUTED rows are
    excluded on purpose: the reconciler established that order never reached
    the market, so it must not block a genuine retry. Everything else counts,
    including a closed trade — one signal produces at most one position.

    The bug this replaces: the guard fetched the OLDEST row and tested it for
    NOT_EXECUTED. Once any attempt was written off, the guard consulted that
    dead row forever, so every further tap of the (never-expiring) approve
    link wrote another PENDING trade and inflated recorded exposure without
    bound — until the portfolio circuit breakers halted trading over
    positions that were never placed.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM trades WHERE signal_id=? AND fill_status != 'NOT_EXECUTED' "
            "ORDER BY id DESC LIMIT 1",
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
        pnl = round(pnl, 2)
        exit_time = now_ist()
        conn.execute(
            "UPDATE trades SET exit_price=?, exit_time=?, pnl=? WHERE id=?",
            (exit_price, exit_time, pnl, trade_id),
        )

    # Mirror the exit (sell) to the Google Sheets "Trades" tab — fills in the
    # exit price / time / P&L on the row opened by append_trade. Non-blocking.
    try:
        from sheets.trade_logger import close_trade_row
        close_trade_row(trade_id, exit_price, exit_time, pnl)
    except Exception:
        pass

    # The exit removed a position — refresh the "Positions" tab snapshot.
    try:
        from sheets.trade_logger import refresh_positions
        refresh_positions(get_open_positions())
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Portfolio queries (used by Risk Sizer)
# ---------------------------------------------------------------------------

def get_open_positions(mode: str | None = None) -> list[dict]:
    """
    Trades that have an entry and no exit yet, excluding orders the reconciler
    established were never placed.

    PENDING rows ARE included on purpose: an approved-but-unreconciled order is
    real exposure as far as the portfolio gate and the Risk Sizer are concerned,
    and treating it as such is what stops a batch of same-day approvals from
    each sizing as though the others did not exist.

    mode: pass "PAPER" or "LIVE" to see only that book's exposure — any risk
    or sizing decision must do this, since paper and live are not the same
    money. Leave as None for an all-modes view (the position monitor, which
    alerts on both books, and the Sheets mirror).
    """
    query = ("SELECT * FROM trades "
             "WHERE entry_price IS NOT NULL AND exit_price IS NULL "
             "  AND fill_status != 'NOT_EXECUTED'")
    params: tuple = ()
    if mode is not None:
        query += " AND mode = ?"
        params = (mode,)
    with get_db() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def get_weekly_pnl(mode: str | None = None) -> float:
    """
    Sum of closed trade P&L since the start of the current week (Monday
    00:00 IST).

    Computed explicitly in Python rather than the old
    date('now', 'weekday 1', '-7 days'): SQLite's "weekday 1" modifier is a
    no-op when 'now' already falls on a Monday, so the unconditional
    "-7 days" that followed landed on the PREVIOUS Monday instead of today's
    — reproduced running on Monday 21 September 2026, which selected 14
    September. It also measured "now" in UTC (SQLite's default), not IST, a
    boundary error of up to ~5.5 hours that this codebase already treats as
    a bug elsewhere (see the UTC/IST fix in get_cycle_log()). Deriving
    "today" from IST directly fixes both at once (2026-09-19 review, item 4).

    mode: pass "PAPER" or "LIVE" to scope the weekly-loss circuit breaker to
    one book. None sums both (used only for the /status DB-connectivity
    check, which does not act on the number).
    """
    today_ist = datetime.now(IST).date()
    week_start = (today_ist - timedelta(days=today_ist.weekday())).isoformat()

    query = ("SELECT COALESCE(SUM(pnl), 0) as total "
             "FROM trades "
             "WHERE exit_time >= ? "
             "  AND pnl IS NOT NULL")
    params: tuple = (week_start,)
    if mode is not None:
        query += " AND mode = ?"
        params = params + (mode,)
    with get_db() as conn:
        row = conn.execute(query, params).fetchone()
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
    """
    Return all cycle_log rows from the last N days, newest first.

    The cutoff is computed in Python against IST, not via SQLite's
    datetime('now', '-N days'). cycle_at is stored as a naive IST wall-clock
    string (see now_ist()), but SQLite's datetime('now') returns UTC — as a
    raw string comparison, that understates the cutoff by the +5:30 offset,
    which OVERSTATES how far back "N days" actually reaches (a "days=1"
    window ends up spanning ~29.5 real hours, not 24). That's exactly how a
    signal from ~27 hours ago showed up in a "Top 5 scores today" table a
    full calendar day after it actually fired — it was never re-evaluated
    that day, it just hadn't aged out of the oversized window yet. See
    get_signal_log() above, which already used this correct pattern.
    """
    cutoff = (datetime.now(IST) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM cycle_log
            WHERE cycle_at >= ?
            ORDER BY cycle_at DESC, ticker
            """,
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_signal_log(days: int = 7) -> list[dict]:
    """
    Return all `signals` rows from the last N days, newest first.

    This is the "why did QC block this" answer — the researcher's own
    rationale sits next to QC's full verdict + rationale, untruncated
    (the daily email only ever prints 120-300 char previews of these).

    created_at is stored as a naive IST string (see now_ist()), so the
    cutoff is computed in Python against IST rather than SQLite's
    datetime('now') (UTC) to avoid a ~5.5h skew in the window.
    """
    cutoff = (datetime.now(IST) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM signals
            WHERE created_at >= ?
            ORDER BY created_at DESC
            """,
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Shadow price checks — grading every "no trade happened" outcome against
# what the price actually did, without ever placing an order.
# ---------------------------------------------------------------------------

# Every status worth a shadow check: any point in the pipeline where a
# candidate that cleared the confidence bar did NOT end up as a trade.
# The reason differs — QC refused it, QC was unreachable, nobody answered
# the alert, you explicitly rejected it, the order never reached the
# market, the sizer/min-size math dropped it before it ever got sized —
# but the underlying question is identical every time: "was not trading
# this one actually right?" Grading all of them the same way, every week,
# is how the whole system — QC's calibration, the sizer's thresholds, even
# whether YOU tend to reject the ones that would have won — gets checked
# against reality instead of running forever on vibes. This is exactly as
# important once real money is on the line, arguably more so: a pattern of
# good candidates dying at the sizer or going unanswered is capital being
# left on the table for no better reason than friction.
#
# Left out, and why:
#   PENDING / APPROVED — not yet resolved. The eventual terminal status
#     (NOT_EXECUTED, EXECUTED, ...) is what gets graded; checking a
#     signal that might still become a real trade would double-count it.
#   EXECUTED — already graded for real, via trades.pnl. A hypothetical
#     shadow return next to an actual fill would be redundant and confusing.
#   DROPPED_NO_PRICE — no price_at_signal exists by construction (the LTP
#     fetch itself failed), so there is nothing to compare against. The
#     price_at_signal IS NOT NULL filter below excludes it either way.
SHADOW_CHECK_STATUSES = (
    "QC_BLOCKED", "QC_ERROR", "NO_RESPONSE", "REJECTED", "NOT_EXECUTED",
    "SKIPPED", "DROPPED_SUBMIN", "DROPPED_SIZER",
)


def get_signals_needing_shadow_check(horizon_days: int, max_age_days: int = 14) -> list[dict]:
    """
    Signals in SHADOW_CHECK_STATUSES old enough for a `horizon_days` shadow
    check that haven't had one recorded yet.

    Bounded by max_age_days so a gap in the scheduler (deploy downtime, a
    paused service) doesn't cause a months-old signal to suddenly get
    checked against today's price as if that were N days out — a stale
    comparison is worse than no comparison.
    """
    now = datetime.now(IST)
    due_before = (now - timedelta(days=horizon_days)).strftime("%Y-%m-%d %H:%M:%S")
    too_old_before = (now - timedelta(days=max_age_days)).strftime("%Y-%m-%d %H:%M:%S")
    placeholders = ",".join("?" * len(SHADOW_CHECK_STATUSES))
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT s.* FROM signals s
            WHERE s.status IN ({placeholders})
              AND s.price_at_signal IS NOT NULL
              AND s.created_at <= ?
              AND s.created_at >= ?
              AND NOT EXISTS (
                  SELECT 1 FROM signal_shadow_checks c
                  WHERE c.signal_id = s.id AND c.horizon_days = ?
              )
            """,
            (*SHADOW_CHECK_STATUSES, due_before, too_old_before, horizon_days),
        ).fetchall()
        return [dict(r) for r in rows]


def record_shadow_check(
    signal_id: int, horizon_days: int, price_at_check: float,
    return_pct: float, notes: str | None = None,
) -> None:
    """Record one horizon's shadow-check result. Safe to call once per
    (signal_id, horizon_days) — later calls for the same pair are no-ops."""
    with get_db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO signal_shadow_checks (
                signal_id, horizon_days, checked_at, price_at_check, return_pct, notes
            ) VALUES (?,?,?,?,?,?)
            """,
            (signal_id, horizon_days, now_ist(), price_at_check, return_pct, notes),
        )


def get_shadow_checks_for_signal_ids(signal_ids: list[int]) -> list[dict]:
    """All recorded shadow checks for a set of signal IDs, oldest horizon first."""
    if not signal_ids:
        return []
    with get_db() as conn:
        placeholders = ",".join("?" * len(signal_ids))
        rows = conn.execute(
            f"""
            SELECT * FROM signal_shadow_checks
            WHERE signal_id IN ({placeholders})
            ORDER BY signal_id, horizon_days
            """,
            signal_ids,
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Portfolio risk helpers (used by risk_manager/portfolio_risk.py)
# ---------------------------------------------------------------------------

def get_all_time_pnl(mode: str) -> float:
    """
    Sum of P&L from every closed trade ever, in ONE book.

    mode is required and must be "PAPER" or "LIVE" — this number feeds the
    drawdown circuit breaker, and silently summing both books together was
    exactly the bug in the 2026-09-19 review: a live loss could offset a
    paper high-water mark and vice versa.
    """
    if mode not in ("PAPER", "LIVE"):
        raise ValueError(f"mode must be 'PAPER' or 'LIVE', got {mode!r}")
    with get_db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) AS total FROM trades "
            "WHERE pnl IS NOT NULL AND mode = ?",
            (mode,),
        ).fetchone()
        return float(row["total"])


def get_update_portfolio_peak(current_value: float, mode: str) -> float:
    """
    Returns the all-time peak portfolio value for ONE book (PAPER or LIVE).
    If current_value is a new high for that book, updates its stored peak.

    mode is required for the same reason as get_all_time_pnl: a shared peak
    let one book's drawdown recovery paper over the other book's losing
    streak.
    """
    if mode not in ("PAPER", "LIVE"):
        raise ValueError(f"mode must be 'PAPER' or 'LIVE', got {mode!r}")
    with get_db() as conn:
        row = conn.execute(
            "SELECT peak_value FROM portfolio_peak WHERE mode = ?", (mode,)
        ).fetchone()
        peak = float(row["peak_value"]) if row else current_value
        if current_value >= peak:
            conn.execute(
                """
                INSERT INTO portfolio_peak (mode, peak_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(mode) DO UPDATE SET peak_value=excluded.peak_value,
                                                updated_at=excluded.updated_at
                """,
                (mode, current_value, now_ist()),
            )
            return current_value
        return peak
