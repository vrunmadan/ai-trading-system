"""
Google Sheets trade logger — mirrors the SQLite ledger in a human-readable spreadsheet.

Every decision is already in SQLite (the authoritative store). Sheets is a
convenience layer so you can see trades, signals, and audit summaries without
running SQL queries.

ONE-TIME SETUP:
  1. GCP project with Sheets API + Drive API enabled. ✅ Done.
  2. OAuth 2.0 Desktop client credentials file at credentials/oauth_client.json. ✅ Done.
  3. Run `python setup/authorize_sheets.py` once — a browser tab opens, you
     log in with your Google account, and the token is cached at
     credentials/authorized_user.json.  After that, the pipeline authenticates
     silently using the cached token (auto-refreshed by gspread).
  4. Create a Google Sheet and set in .env:
       SHEETS_OAUTH_CLIENT_PATH=credentials/oauth_client.json
       SHEETS_AUTHORIZED_USER_PATH=credentials/authorized_user.json
       SHEETS_SPREADSHEET_ID=your_spreadsheet_id

Tabs maintained (auto-created if missing):
  Trades    — one row per executed trade; exit/P&L filled in when trade closes
  Signals   — one row per generated signal
  Positions — live snapshot of open positions (overwritten each cycle)
  Audits    — weekly audit summaries from the Auditor (Gemini)

All public functions fail silently with a log.warning() if Sheets is not
configured — the pipeline never breaks because of a missing spreadsheet.
"""

import base64
import logging
import os
import tempfile
from datetime import datetime
from typing import Optional

import pytz

log = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

OAUTH_CLIENT_PATH    = os.getenv("SHEETS_OAUTH_CLIENT_PATH", "credentials/oauth_client.json")
AUTHORIZED_USER_PATH = os.getenv("SHEETS_AUTHORIZED_USER_PATH", "credentials/authorized_user.json")
SPREADSHEET_ID       = os.getenv("SHEETS_SPREADSHEET_ID", "")

# Railway env vars — base64-encoded credential files written during setup.
# On Railway, the credentials/ directory doesn't exist, so we decode these
# env vars and write them to /tmp/ on startup instead.
_OAUTH_CLIENT_B64    = os.getenv("SHEETS_OAUTH_CLIENT_B64", "")
_AUTHORIZED_USER_B64 = os.getenv("SHEETS_AUTHORIZED_USER_B64", "")

# Preferred for a server app: a Google SERVICE ACCOUNT. Unlike OAuth "authorized
# user" tokens (which expire — weekly if the consent screen is in Testing mode,
# the invalid_grant we hit), a service account never expires and needs no
# browser re-auth. Set SHEETS_SERVICE_ACCOUNT_B64 (base64 of the downloaded JSON
# key) or SHEETS_SERVICE_ACCOUNT_JSON (raw JSON) in Railway, and share the sheet
# with the service account's client_email. When present it takes priority.
_SERVICE_ACCOUNT_B64  = os.getenv("SHEETS_SERVICE_ACCOUNT_B64", "")
_SERVICE_ACCOUNT_JSON = os.getenv("SHEETS_SERVICE_ACCOUNT_JSON", "")

# Temp paths used when running on Railway (overrides file-based paths above)
_TMP_OAUTH_CLIENT    = os.path.join(tempfile.gettempdir(), "sheets_oauth_client.json")
_TMP_AUTHORIZED_USER = os.path.join(tempfile.gettempdir(), "sheets_authorized_user.json")


def _write_credentials_from_env() -> bool:
    """
    Decode SHEETS_OAUTH_CLIENT_B64 and SHEETS_AUTHORIZED_USER_B64 from env
    and write them to /tmp/. Called once per process start when files don't exist.
    Returns True if credentials are now available (either from files or env vars).
    """
    if os.path.exists(AUTHORIZED_USER_PATH):
        return True  # Local files present — no action needed

    if not _OAUTH_CLIENT_B64 or not _AUTHORIZED_USER_B64:
        return False  # Neither files nor env vars — Sheets won't work

    try:
        with open(_TMP_OAUTH_CLIENT, "w") as f:
            f.write(base64.b64decode(_OAUTH_CLIENT_B64).decode("utf-8"))
        with open(_TMP_AUTHORIZED_USER, "w") as f:
            f.write(base64.b64decode(_AUTHORIZED_USER_B64).decode("utf-8"))
        log.info("Sheets credentials decoded from env vars to /tmp/.")
        return True
    except Exception as e:
        log.error(f"Failed to decode Sheets credentials from env vars: {e}")
        return False


def _resolve_cred_paths() -> tuple[str, str]:
    """Return the actual credential file paths to use (local or /tmp/)."""
    if os.path.exists(AUTHORIZED_USER_PATH):
        return OAUTH_CLIENT_PATH, AUTHORIZED_USER_PATH
    return _TMP_OAUTH_CLIENT, _TMP_AUTHORIZED_USER


def _service_account_info() -> Optional[dict]:
    """Service-account key parsed from env (B64 or raw JSON), or None if unset."""
    import json
    raw = ""
    if _SERVICE_ACCOUNT_B64:
        try:
            raw = base64.b64decode(_SERVICE_ACCOUNT_B64).decode("utf-8")
        except Exception as e:
            log.error(f"Could not base64-decode SHEETS_SERVICE_ACCOUNT_B64: {e}")
            return None
    elif _SERVICE_ACCOUNT_JSON:
        raw = _SERVICE_ACCOUNT_JSON
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except Exception as e:
        log.error(f"SHEETS_SERVICE_ACCOUNT_* is not valid JSON: {e}")
        return None

TAB_TRADES    = "Trades"
TAB_SIGNALS   = "Signals"
TAB_POSITIONS = "Positions"
TAB_AUDITS    = "Audits"

HEADERS: dict[str, list[str]] = {
    TAB_TRADES: [
        "Trade ID", "Signal ID", "Date", "Ticker", "Direction",
        "Quantity", "Entry Price (₹)", "Entry Time",
        "Exit Price (₹)", "Exit Time", "P&L (₹)", "Mode",
    ],
    TAB_SIGNALS: [
        "Signal ID", "Date", "Ticker", "Regime", "Strategy",
        "Direction", "Tech Score", "Fund Score", "Confidence",
        "QC Verdict", "Sized Qty", "Status", "Rationale (truncated)",
    ],
    TAB_POSITIONS: [
        "Ticker", "Direction", "Quantity", "Entry Price (₹)",
        "Entry Time", "Capital Deployed (₹)", "Mode", "Signal ID",
    ],
    TAB_AUDITS: [
        "Week Start", "Week End", "Summary",
        "Confidence Analysis", "Missed Opportunities",
        "Hypothesis Backlog", "Model",
    ],
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _configured() -> bool:
    """Return True if all required config is present."""
    if not SPREADSHEET_ID:
        log.warning(
            "Google Sheets not configured — set SHEETS_SPREADSHEET_ID in .env to enable trade logging."
        )
        return False
    # A service account is self-sufficient — no OAuth files needed.
    if _service_account_info() is not None:
        return True
    if not _write_credentials_from_env():
        log.warning(
            "Sheets credentials not found. Set SHEETS_SERVICE_ACCOUNT_B64 (recommended — "
            "no expiry), or run `python setup/authorize_sheets.py` locally, or set "
            "SHEETS_OAUTH_CLIENT_B64 and SHEETS_AUTHORIZED_USER_B64 in Railway env vars."
        )
        return False
    return True


def _get_client():
    """Return an authenticated gspread client, or None on failure.

    Prefers a service account (no token expiry); falls back to OAuth files.
    """
    try:
        import gspread
        info = _service_account_info()
        if info is not None:
            return gspread.service_account_from_dict(info)
        oauth_path, auth_path = _resolve_cred_paths()
        return gspread.oauth(
            credentials_filename=oauth_path,
            authorized_user_filename=auth_path,
        )
    except Exception as e:
        log.error(f"gspread auth failed: {e}")
        return None


def _open_spreadsheet(client):
    """Open the configured spreadsheet, or None on failure."""
    try:
        return client.open_by_key(SPREADSHEET_ID)
    except Exception as e:
        log.error(f"Could not open spreadsheet {SPREADSHEET_ID}: {e}")
        return None


def _get_or_create_tab(wb, tab_name: str):
    """
    Return the worksheet named tab_name. Creates it (with header row) if absent.
    Returns None on any failure.
    """
    try:
        ws = wb.worksheet(tab_name)
        return ws
    except Exception:
        pass  # worksheet doesn't exist yet — create it

    try:
        ws = wb.add_worksheet(title=tab_name, rows=1000, cols=20)
        ws.append_row(HEADERS[tab_name], value_input_option="USER_ENTERED")
        log.info(f"Created Sheets tab: {tab_name}")
        return ws
    except Exception as e:
        log.error(f"Failed to create tab '{tab_name}': {e}")
        return None


def _find_row_by_id(ws, id_value: int, id_col: int = 1) -> Optional[int]:
    """
    Return the 1-based row number where column id_col equals str(id_value).
    Returns None if not found.
    """
    try:
        cell = ws.find(str(id_value), in_column=id_col)
        return cell.row if cell else None
    except Exception:
        return None


def _now_ist() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def _date_ist() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def append_signal(
    signal_id: int,
    signal,           # TradeSignal from signal_generator
    sizing,           # SizingResult from risk_sizer
    qc_verdict,       # QCVerdict from qc_factchecker (or None)
    status: str = "PENDING",   # real ledger status (QC_BLOCKED, QC_ERROR, DROPPED_*, ...)
) -> None:
    """Append one row to the Signals tab for a newly generated signal."""
    if not _configured():
        return
    client = _get_client()
    if not client:
        return
    wb = _open_spreadsheet(client)
    if not wb:
        return
    ws = _get_or_create_tab(wb, TAB_SIGNALS)
    if not ws:
        return

    try:
        row = [
            signal_id,
            _date_ist(),
            signal.ticker,
            signal.regime.value,
            signal.strategy_bucket,
            signal.direction,
            round(signal.technical_score, 1),
            round(signal.fundamental_score, 1),
            round(signal.confidence_score, 1),
            qc_verdict.verdict if qc_verdict else "N/A",
            sizing.quantity if sizing else 0,
            status,
            signal.rationale[:300],
        ]
        ws.append_row(row, value_input_option="USER_ENTERED")
        log.info(f"Sheets: signal #{signal_id} ({signal.ticker}) logged.")
    except Exception as e:
        log.warning(f"Sheets signal log failed: {e}")


def append_trade(
    trade_id: int,
    signal_id: int,
    ticker: str,
    direction: str,
    quantity: int,
    entry_price: float,
    entry_time: str,
    mode: str = "PAPER",
) -> None:
    """Append one row to the Trades tab when a trade is opened."""
    if not _configured():
        return
    client = _get_client()
    if not client:
        return
    wb = _open_spreadsheet(client)
    if not wb:
        return
    ws = _get_or_create_tab(wb, TAB_TRADES)
    if not ws:
        return

    try:
        row = [
            trade_id, signal_id, _date_ist(), ticker, direction,
            quantity, round(entry_price, 2), entry_time,
            "", "", "", mode,  # exit fields blank until trade closes
        ]
        ws.append_row(row, value_input_option="USER_ENTERED")
        log.info(f"Sheets: trade #{trade_id} ({ticker}) opened.")
    except Exception as e:
        log.warning(f"Sheets trade log failed: {e}")


def close_trade_row(
    trade_id: int,
    exit_price: float,
    exit_time: str,
    pnl: float,
) -> None:
    """Find the Trades row for trade_id and fill in exit price, time, and P&L."""
    if not _configured():
        return
    client = _get_client()
    if not client:
        return
    wb = _open_spreadsheet(client)
    if not wb:
        return
    ws = _get_or_create_tab(wb, TAB_TRADES)
    if not ws:
        return

    try:
        row_num = _find_row_by_id(ws, trade_id, id_col=1)
        if not row_num:
            log.warning(f"Sheets: trade #{trade_id} not found — can't update exit.")
            return

        # Columns 9 (Exit Price), 10 (Exit Time), 11 (P&L) — 1-based
        ws.update(
            f"I{row_num}:K{row_num}",
            [[round(exit_price, 2), exit_time, round(pnl, 2)]],
            value_input_option="USER_ENTERED",
        )
        log.info(f"Sheets: trade #{trade_id} closed (P&L ₹{pnl:,.2f}).")
    except Exception as e:
        log.warning(f"Sheets close_trade update failed: {e}")


def refresh_positions(open_positions: list) -> None:
    """
    Overwrite the Positions tab with the current snapshot of open trades.
    open_positions: list of dicts from ledger.db.get_open_positions()
                    (keys: id, signal_id, ticker, direction, quantity,
                           entry_price, entry_time, mode)
    """
    if not _configured():
        return
    client = _get_client()
    if not client:
        return
    wb = _open_spreadsheet(client)
    if not wb:
        return
    ws = _get_or_create_tab(wb, TAB_POSITIONS)
    if not ws:
        return

    try:
        # Clear everything below the header row
        ws.resize(rows=1)
        ws.resize(rows=1000)

        if not open_positions:
            return

        rows = []
        for p in open_positions:
            deployed = float(p.get("entry_price", 0)) * int(p.get("quantity", 0))
            rows.append([
                p.get("ticker", ""),
                p.get("direction", ""),
                p.get("quantity", 0),
                round(float(p.get("entry_price", 0)), 2),
                p.get("entry_time", ""),
                round(deployed, 2),
                p.get("mode", "PAPER"),
                p.get("signal_id", ""),
            ])

        ws.append_rows(rows, value_input_option="USER_ENTERED")
        log.info(f"Sheets: Positions tab refreshed ({len(rows)} open positions).")
    except Exception as e:
        log.warning(f"Sheets positions refresh failed: {e}")


def append_audit(
    week_start: str,
    week_end: str,
    summary: str,
    confidence_analysis: str = "",
    missed_opportunities: str = "",
    hypothesis_backlog: str = "",
    model_used: str = "",
) -> None:
    """Append one row to the Audits tab after a weekly audit completes."""
    if not _configured():
        return
    client = _get_client()
    if not client:
        return
    wb = _open_spreadsheet(client)
    if not wb:
        return
    ws = _get_or_create_tab(wb, TAB_AUDITS)
    if not ws:
        return

    try:
        row = [
            week_start, week_end,
            summary[:500],
            confidence_analysis[:300],
            missed_opportunities[:300],
            hypothesis_backlog[:300],
            model_used,
        ]
        ws.append_row(row, value_input_option="USER_ENTERED")
        log.info(f"Sheets: audit for {week_start}–{week_end} logged.")
    except Exception as e:
        log.warning(f"Sheets audit log failed: {e}")
