"""
Trade reconciler — runs at EOD, before the position monitor.

The approve path records *intent*: when you tap Approve, a PENDING row is
written to `trades` using the sized quantity, and you are redirected to a
pre-filled Kite basket. Whether you actually tapped Place Order is something
only Kite knows.

This module closes that gap. For every PENDING trade it asks Kite what you
actually hold and either:

    CONFIRMED     found in positions()/holdings() — entry_price is replaced
                  with the real average price, quantity with the real fill
    NOT_EXECUTED  not found — the row stays for the audit trail but stops
                  counting toward exposure

Why both positions() and holdings():
  A CNC (delivery) buy shows up in positions()['net'] on the trading day it
  was placed, and moves to holdings() after settlement. Reconciling the same
  evening usually hits positions(); a run that catches up on an older PENDING
  row (missed job, redeploy) needs holdings(). Checking both makes the job
  safe to re-run and safe to miss.

Ordering matters: this must run BEFORE monitor.check_open_positions(), so
stop-losses are evaluated against confirmed fills rather than assumed ones.
"""

import logging
import os

log = logging.getLogger(__name__)

# A PENDING row older than this many days with no sign of a fill is written off.
# Two trading days covers a Friday approval reconciled on Monday.
MAX_PENDING_AGE_DAYS = int(os.getenv("MAX_PENDING_AGE_DAYS", "2"))


def _position_key(exchange: str, tradingsymbol: str) -> str:
    return f"{(exchange or 'NSE').upper()}:{(tradingsymbol or '').upper()}"


def _build_holdings_map(kite) -> tuple[dict[str, dict], bool, bool]:
    """
    Merge kite.positions()['net'] and kite.holdings() into one
    "EXCHANGE:SYMBOL" -> {quantity, average_price, source} map.

    positions() wins when a symbol appears in both, because it reflects
    today's activity. Each source is fetched independently so one failing
    does not blind the other — but the caller must know WHICH sources
    actually succeeded. A symbol's absence from the merged map is only
    trustworthy evidence of "not held" when BOTH sources are known-good;
    if either call failed, that absence is unverified, not a fact. Before
    this fix, holdings()/positions() failures were swallowed here and the
    caller saw only an empty (or partial) map indistinguishable from a
    confirmed-empty account — reproduced in the 2026-09-19 review as a
    LIVE row getting marked NOT_EXECUTED purely because both Kite calls
    happened to fail on that run.

    Returns (merged, holdings_ok, positions_ok).
    """
    merged: dict[str, dict] = {}
    holdings_ok = False
    positions_ok = False

    try:
        holdings = kite.holdings() or []
        holdings_ok = True
        for h in holdings:
            qty = int(h.get("quantity") or 0)
            if qty <= 0:
                continue
            merged[_position_key(h.get("exchange"), h.get("tradingsymbol"))] = {
                "quantity": qty,
                "average_price": float(h.get("average_price") or 0.0),
                "source": "holdings",
            }
    except Exception as e:
        log.warning(f"Could not fetch Kite holdings: {e}")

    try:
        positions = (kite.positions() or {}).get("net", []) or []
        positions_ok = True
        for p in positions:
            qty = int(p.get("quantity") or 0)
            if qty <= 0:
                continue
            merged[_position_key(p.get("exchange"), p.get("tradingsymbol"))] = {
                "quantity": qty,
                "average_price": float(p.get("average_price") or 0.0),
                "source": "positions",
            }
    except Exception as e:
        log.warning(f"Could not fetch Kite positions: {e}")

    return merged, holdings_ok, positions_ok


def _pending_age_days(entry_time: str | None) -> int:
    """Whole days since the PENDING row was written. 0 if unparseable."""
    if not entry_time:
        return 0
    from datetime import datetime

    from ledger.db import IST

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            written = datetime.strptime(entry_time[:19], fmt)
            return max(0, (datetime.now(IST).replace(tzinfo=None) - written).days)
        except ValueError:
            continue
    return 0


def reconcile_pending_trades() -> dict:
    """
    Reconcile every PENDING trade. PAPER rows are simulated unconditionally;
    LIVE rows are checked against Kite.

    Returns a summary dict: {"pending": n, "confirmed": n, "not_executed": n,
    "deferred": n, "skipped": bool}. Never raises — a reconciliation failure
    must not take down the EOD job that also runs the position monitor.
    """
    from ledger.db import (
        confirm_trade,
        get_pending_trades,
        mark_signal_executed,
        mark_signal_not_executed,
        mark_trade_not_executed,
    )

    summary = {"pending": 0, "confirmed": 0, "not_executed": 0,
               "deferred": 0, "simulated": 0, "skipped": False}

    pending = get_pending_trades()
    summary["pending"] = len(pending)
    if not pending:
        log.info("Reconciler: no PENDING trades.")
        return summary

    lines: list[str] = []

    # PAPER rows first, and unconditionally — checked BEFORE any Kite lookup
    # and regardless of whether Kite is even reachable. A paper approval is a
    # simulation: no order was ever sent, so there is nothing in Kite that
    # could legitimately confirm it, and nothing there should ever be allowed
    # to. Before this fix, a PAPER row was matched against Kite FIRST, so a
    # coincidental real holding under the same ticker/exchange — the user's
    # own pre-existing position, or (before the approval-side fix) a real
    # order placed through a paper-mode approval link — would be adopted as
    # the "confirmed" fill, and the real holding could later be closed by a
    # simulation while it stayed open at the broker (2026-09-19 review, item 1).
    live_pending = []
    for trade in pending:
        if (trade.get("mode") or "PAPER").upper() != "PAPER":
            live_pending.append(trade)
            continue
        trade_id = trade["id"]
        ticker = trade["ticker"]
        exchange = trade.get("exchange") or "NSE"
        want_qty = int(trade["quantity"])
        sim_price = float(trade["entry_price"] or 0.0)
        note = (
            f"Simulated fill at ₹{sim_price:,.2f} (PAPER mode — no order "
            f"was placed, so nothing was verified against Kite)."
        )
        confirm_trade(trade_id, sim_price, note=note)
        _promote_signal(trade, mark_signal_executed)
        summary["confirmed"] += 1
        summary["simulated"] += 1
        lines.append(
            f"📝 SIMULATED  {exchange}:{ticker}  {want_qty} @ "
            f"₹{sim_price:,.2f}  (paper)"
        )
        log.info(f"Reconciler: trade {trade_id} {_position_key(exchange, ticker)} "
                 f"-> CONFIRMED (simulated).")

    if not live_pending:
        log.info(f"Reconciler: {summary['simulated']} paper trade(s) simulated, "
                 f"no LIVE trades to check against Kite.")
        if lines:
            _alert(
                f"Trade reconciliation — {summary['confirmed']} confirmed, "
                f"{summary['not_executed']} not placed",
                "Daily reconciliation of approved trades\n"
                + "─" * 40 + "\n" + "\n".join(lines),
            )
        return summary

    log.info(f"Reconciler: {len(live_pending)} LIVE PENDING trade(s) to check against Kite.")

    try:
        from trader.kite_client import get_kite_client

        kite = get_kite_client()
    except Exception as e:
        # Fail closed: leave LIVE rows PENDING so the next run retries. Marking
        # them NOT_EXECUTED here would silently erase real exposure. This must
        # not affect the PAPER rows already simulated above — a Kite outage
        # has nothing to do with paper trading and must not stall it.
        log.error(f"Reconciler: cannot reach Kite — leaving {len(live_pending)} LIVE "
                  f"row(s) PENDING for the next run: {e}")
        summary["skipped"] = True
        _alert(
            "Trade reconciliation did NOT run for LIVE trades",
            f"{len(live_pending)} approved LIVE trade(s) could not be verified against "
            f"Kite ({e}).\n\nThey remain PENDING and still count toward exposure. "
            f"The next EOD run will retry."
        )
        return summary

    held, holdings_ok, positions_ok = _build_holdings_map(kite)

    if not (holdings_ok or positions_ok):
        # Both calls failed even though the client itself was built fine —
        # e.g. an expired token that constructs OK but rejects every real
        # request. This is functionally the same failure as Kite being
        # unreachable at all: we have zero visibility into the account, so
        # fail closed the same way rather than let every LIVE row's "no
        # match" look like a genuine absence.
        log.error(f"Reconciler: both Kite holdings() and positions() failed — "
                  f"leaving {len(live_pending)} LIVE row(s) PENDING for the next run.")
        summary["skipped"] = True
        _alert(
            "Trade reconciliation did NOT run for LIVE trades",
            f"Kite holdings() and positions() both failed, so nothing could be "
            f"verified. {len(live_pending)} approved LIVE trade(s) remain PENDING "
            f"and still count toward exposure. The next EOD run will retry."
        )
        return summary

    snapshot_complete = holdings_ok and positions_ok
    if not snapshot_complete:
        missing = "holdings()" if not holdings_ok else "positions()"
        log.warning(
            f"Reconciler: Kite {missing} failed — this run's snapshot is "
            f"incomplete. Unmatched LIVE rows will be deferred, not written off."
        )
    if not held:
        log.warning("Reconciler: Kite returned no positions or holdings at all.")

    for trade in live_pending:
        trade_id = trade["id"]
        ticker = trade["ticker"]
        exchange = trade.get("exchange") or "NSE"
        want_qty = int(trade["quantity"])
        key = _position_key(exchange, ticker)
        match = held.get(key)

        if match:
            # Claim only what we asked for. A larger holding means the user
            # already owned some of this name, and that surplus is not ours.
            fill_qty = min(want_qty, match["quantity"])
            fill_price = match["average_price"] or float(trade["entry_price"] or 0.0)
            partial = fill_qty < want_qty
            note = (
                f"Confirmed from Kite {match['source']} at "
                f"₹{fill_price:,.2f}"
                + (f" (partial: {fill_qty}/{want_qty})" if partial else "")
            )
            confirm_trade(trade_id, fill_price, quantity=fill_qty, note=note)
            _promote_signal(trade, mark_signal_executed)
            summary["confirmed"] += 1
            lines.append(
                f"✅ CONFIRMED  {exchange}:{ticker}  {fill_qty} @ "
                f"₹{fill_price:,.2f}" + ("  (PARTIAL FILL)" if partial else "")
            )
            log.info(f"Reconciler: trade {trade_id} {key} -> CONFIRMED. {note}")
            continue

        if not snapshot_complete:
            # We cannot tell "not found" from "couldn't check" this run. A
            # real position must never be written off on unverified
            # information — leave it PENDING regardless of age.
            summary["deferred"] += 1
            lines.append(
                f"⚠️ UNVERIFIED  {exchange}:{ticker}  {want_qty} "
                f"— Kite snapshot was incomplete this run, will re-check"
            )
            log.warning(
                f"Reconciler: trade {trade_id} {key} not found, but this run's "
                f"Kite snapshot was incomplete — leaving PENDING rather than "
                f"risk writing off a real position."
            )
            continue

        # LIVE row confirmed absent from a COMPLETE snapshot. Give it a grace
        # period before writing it off, so a settlement lag or a skipped run
        # does not erase a real position.
        age = _pending_age_days(trade.get("entry_time"))
        if age < MAX_PENDING_AGE_DAYS:
            summary["deferred"] += 1
            lines.append(
                f"⏳ PENDING    {exchange}:{ticker}  {want_qty} "
                f"— not in Kite yet (day {age}), will re-check"
            )
            log.info(f"Reconciler: trade {trade_id} {key} not found, age {age}d "
                     f"< {MAX_PENDING_AGE_DAYS}d — leaving PENDING.")
            continue

        reason = (
            f"Not found in Kite positions or holdings {age} day(s) after approval "
            f"— treating as never placed."
        )
        mark_trade_not_executed(trade_id, reason)
        _promote_signal(trade, mark_signal_not_executed)
        summary["not_executed"] += 1
        lines.append(
            f"❌ NOT PLACED {exchange}:{ticker}  {want_qty} — {age}d with no fill"
        )
        log.warning(f"Reconciler: trade {trade_id} {key} -> NOT_EXECUTED. {reason}")

    if lines:
        _alert(
            f"Trade reconciliation — {summary['confirmed']} confirmed, "
            f"{summary['not_executed']} not placed",
            "Daily reconciliation of approved trades against your Kite account\n"
            + "─" * 40 + "\n" + "\n".join(lines)
            + "\n\nNOT PLACED rows stop counting toward exposure. If one of those "
              "is wrong, you placed the order outside the pre-filled basket and "
              "the ledger could not match it.",
        )

    log.info(
        f"Reconciler done: {summary['confirmed']} confirmed, "
        f"{summary['not_executed']} not executed, {summary['deferred']} still pending."
    )
    return summary


def _promote_signal(trade: dict, fn) -> None:
    """
    Mirror a trade's fill outcome onto its signal row.

    signals.status is what the weekly Auditor reads, so it must not claim a
    trade executed until the fill is established. Never fatal: a trade row
    with no signal_id (a manual insert, say) simply has nothing to update.
    """
    signal_id = trade.get("signal_id")
    if not signal_id:
        return
    try:
        fn(signal_id)
    except Exception as e:
        log.error(f"Could not update status for signal {signal_id}: {e}")


def _alert(subject: str, body: str) -> None:
    try:
        from alerts.gmail_alert import send_plain_email

        send_plain_email(subject=f"\U0001f4d2 {subject}", body=body)
    except Exception as e:
        log.error(f"Reconciler alert email failed: {e}")
