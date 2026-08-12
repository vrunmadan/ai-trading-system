"""
universe/universe_refresher.py
-------------------------------
Weekly universe discovery + validation job.

Two jobs in one:

  DISCOVERY (the main event)
  --------------------------
  Runs a fundamental screen against ALL ~5,000+ listed companies on NSE + BSE.
  Finds stocks that pass our quality filter (ROCE > 20, debt low, sales growing)
  but are NOT in our current universe.csv — these are opportunities we might be
  missing. Surfaces them ranked by market cap with a one-line snapshot.

  VALIDATION (health check on the existing 62)
  --------------------------------------------
  Cross-checks each stock in universe.csv against the same screen results.
  Stocks that no longer appear have slipped below at least one threshold and
  should be reviewed for removal.

Output:
  - Email with an HTML report (new candidates + degraded stocks + confirmed list)
  - Log output for Railway

Triggered:
  - Weekly: Sunday 8:00 PM IST (see webhook_server.py scheduler)
  - Manual: GET /refresh_universe?secret=<APPROVAL_SECRET>

Design principles:
  - Never auto-edits universe.csv. That is always a human decision.
  - Screener is read-only. No writes, no account changes.
  - If Screener is unreachable, logs and emails a failure notice — doesn't crash.
"""

import logging
import os
from datetime import datetime
from typing import Optional

import pytz

log = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# Minimum market cap to appear in "strong new candidate" bucket (vs "watch list")
MIN_STRONG_CANDIDATE_MCAP = float(os.getenv("MIN_CANDIDATE_MCAP_CR", 500))

# Screener query for discovery — can override via env var
DISCOVERY_QUERY = os.getenv(
    "SCREENER_QUERY",
    "ROCE > 20 AND Debt to equity < 0.5 AND Sales growth > 15 AND Market Capitalization > 300",
)


# ---------------------------------------------------------------------------
# Core refresh logic
# ---------------------------------------------------------------------------

def run_universe_refresh(screener_client=None) -> dict:
    """
    Main entry point. Runs the discovery screen + validates existing universe.
    Returns a results dict suitable for format_html_report() / format_text_report().

    screener_client: pre-built ScreenerClient (passed in from webhook endpoint).
                     If None, builds from env vars.
    """
    from universe.loader import load_universe
    from universe.screener_client import build_client_from_env, DISCOVERY_QUERY as _DEFAULT_QUERY

    if screener_client is None:
        screener_client = build_client_from_env()

    # Load current universe
    universe_entries = load_universe()
    universe_symbols = {e.ticker.upper() for e in universe_entries}
    universe_map = {e.ticker.upper(): e for e in universe_entries}

    log.info(f"Current universe: {len(universe_entries)} stocks")
    log.info(f"Running discovery screen: {DISCOVERY_QUERY}")

    # Run the full-market screen
    screen_results = screener_client.run_screen(query=DISCOVERY_QUERY)
    screen_symbols = {r["symbol"].upper() for r in screen_results}

    log.info(f"Screen returned {len(screen_results)} companies across all NSE+BSE listings")

    # Categorise
    new_candidates: list[dict] = []
    confirmed: list[dict] = []
    degraded: list[dict] = []

    # New candidates: in screen, not in our universe
    for r in screen_results:
        sym = r["symbol"].upper()
        if sym not in universe_symbols:
            new_candidates.append(r)

    # Confirmed vs degraded: existing universe stocks vs screen
    for entry in universe_entries:
        sym = entry.ticker.upper()
        if sym in screen_symbols:
            # Find the screen row for this symbol
            screen_row = next((r for r in screen_results if r["symbol"].upper() == sym), {})
            confirmed.append({
                "ticker":       sym,
                "company":      entry.company_name,
                "exchange":     entry.exchange,
                "sector":       entry.sector,
                "market_cap_cr": screen_row.get("market_cap_cr", entry.market_cap_cr),
                "roce":         screen_row.get("roce"),
                "screener_url": screen_row.get("screener_url", ""),
            })
        else:
            degraded.append({
                "ticker":   sym,
                "company":  entry.company_name,
                "exchange": entry.exchange,
                "sector":   entry.sector,
                "notes":    entry.notes,
            })

    # Split new candidates into "strong" (> MIN_STRONG_CANDIDATE_MCAP Cr) and "watch"
    strong_new = [
        r for r in new_candidates
        if r.get("market_cap_cr") and r["market_cap_cr"] >= MIN_STRONG_CANDIDATE_MCAP
    ]
    watch_new = [
        r for r in new_candidates
        if not r.get("market_cap_cr") or r["market_cap_cr"] < MIN_STRONG_CANDIDATE_MCAP
    ]

    # Sort strong new by market cap descending
    strong_new.sort(key=lambda r: r.get("market_cap_cr") or 0, reverse=True)

    log.info(
        f"Results — confirmed: {len(confirmed)} | degraded: {len(degraded)} | "
        f"new strong: {len(strong_new)} | new watch: {len(watch_new)}"
    )

    return {
        "run_at":       datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST"),
        "query":        DISCOVERY_QUERY,
        "total_in_screen": len(screen_results),
        "confirmed":    confirmed,
        "degraded":     degraded,
        "strong_new":   strong_new,   # > MIN_STRONG_CANDIDATE_MCAP Cr — add to universe?
        "watch_new":    watch_new,    # smaller / lower conviction — keep watching
    }


# ---------------------------------------------------------------------------
# Report formatters
# ---------------------------------------------------------------------------

def format_text_report(results: dict) -> str:
    lines = []
    lines.append("=" * 70)
    lines.append("WEEKLY UNIVERSE REFRESH REPORT")
    lines.append(f"Run at: {results['run_at']}")
    lines.append(f"Screen: {results['query']}")
    lines.append(f"Total companies in screen (all NSE+BSE): {results['total_in_screen']}")
    lines.append("=" * 70)
    lines.append(
        f"  ✅ Confirmed (still in screen):     {len(results['confirmed'])}\n"
        f"  ⚠️  Degraded (no longer in screen):  {len(results['degraded'])}\n"
        f"  🆕 New strong candidates (>₹{MIN_STRONG_CANDIDATE_MCAP:.0f}Cr): {len(results['strong_new'])}\n"
        f"  👀 New watch list (<₹{MIN_STRONG_CANDIDATE_MCAP:.0f}Cr):  {len(results['watch_new'])}"
    )
    lines.append("")

    if results["degraded"]:
        lines.append("─" * 70)
        lines.append("⚠️  DEGRADED — no longer pass the screen (review for removal)")
        lines.append("─" * 70)
        for s in results["degraded"]:
            lines.append(f"  {s['ticker']:<16} {s['company']} ({s['exchange']})")
            if s["notes"]:
                lines.append(f"    Note: {s['notes'][:80]}")
        lines.append("")

    if results["strong_new"]:
        lines.append("─" * 70)
        lines.append(f"🆕 NEW STRONG CANDIDATES — pass screen, not in universe (market cap > ₹{MIN_STRONG_CANDIDATE_MCAP:.0f}Cr)")
        lines.append("─" * 70)
        for r in results["strong_new"][:30]:  # top 30
            mcap = f"₹{r['market_cap_cr']:,.0f}Cr" if r.get("market_cap_cr") else "?"
            roce  = f"ROCE {r['roce']:.0f}%" if r.get("roce") else ""
            lines.append(f"  {r['symbol']:<14} {r['company']:<40} {mcap}  {roce}")
            lines.append(f"    → {r.get('screener_url', '')}")
        if len(results["strong_new"]) > 30:
            lines.append(f"  ... and {len(results['strong_new']) - 30} more")
        lines.append("")

    if results["watch_new"]:
        lines.append("─" * 70)
        lines.append(f"👀 WATCH LIST — pass screen, market cap < ₹{MIN_STRONG_CANDIDATE_MCAP:.0f}Cr (smaller/less liquid)")
        lines.append("─" * 70)
        for r in results["watch_new"][:20]:
            mcap = f"₹{r['market_cap_cr']:,.0f}Cr" if r.get("market_cap_cr") else "?"
            lines.append(f"  {r['symbol']:<14} {r['company']:<40} {mcap}")
        if len(results["watch_new"]) > 20:
            lines.append(f"  ... and {len(results['watch_new']) - 20} more")
        lines.append("")

    lines.append("─" * 70)
    lines.append("✅ CONFIRMED — existing universe stocks still passing the screen")
    lines.append("─" * 70)
    for s in results["confirmed"]:
        roce = f"ROCE {s['roce']:.0f}%" if s.get("roce") else ""
        lines.append(f"  {s['ticker']:<14} {s['company']:<40} {roce}")
    lines.append("")
    lines.append("=" * 70)
    return "\n".join(lines)


def format_html_report(results: dict) -> str:
    def _mcap(r):
        v = r.get("market_cap_cr")
        return f"₹{v:,.0f}Cr" if v else "—"

    def _roce(r):
        v = r.get("roce")
        return f"{v:.0f}%" if v else "—"

    degraded_rows = "".join(
        f"<tr>"
        f"<td><b>{s['ticker']}</b></td>"
        f"<td>{s['company']}</td>"
        f"<td>{s['exchange']}</td>"
        f"<td>{s['sector']}</td>"
        f"<td style='color:#888;font-size:12px'>{(s.get('notes') or '')[:80]}</td>"
        f"</tr>"
        for s in results["degraded"]
    )

    def _cmp_cell(r):
        v = r.get("cmp")
        return f"₹{v:,.0f}" if v else "—"

    def _pe_cell(r):
        v = r.get("pe")
        return f"{v:.1f}×" if v else "—"

    strong_rows = "".join(
        "<tr>"
        f"<td><b><a href='{r.get('screener_url', '#')}' target='_blank' style='color:#2563eb'>{r['symbol']}</a></b></td>"
        f"<td>{r['company']}</td>"
        f"<td>{_mcap(r)}</td>"
        f"<td>{_roce(r)}</td>"
        f"<td>{_cmp_cell(r)}</td>"
        f"<td>{_pe_cell(r)}</td>"
        "</tr>"
        for r in results["strong_new"][:40]
    )

    watch_rows = "".join(
        f"<tr>"
        f"<td><b><a href='{r.get('screener_url', '#')}' target='_blank' style='color:#2563eb'>{r['symbol']}</a></b></td>"
        f"<td>{r['company']}</td>"
        f"<td>{_mcap(r)}</td>"
        f"<td>{_roce(r)}</td>"
        f"</tr>"
        for r in results["watch_new"][:30]
    )

    confirmed_rows = "".join(
        f"<tr>"
        f"<td><b>{s['ticker']}</b></td>"
        f"<td>{s['company']}</td>"
        f"<td>{s['exchange']}</td>"
        f"<td>{s['sector']}</td>"
        f"<td>{_mcap(s)}</td>"
        f"<td>{_roce(s)}</td>"
        f"</tr>"
        for s in results["confirmed"]
    )

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Weekly Universe Refresh</title>
<style>
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:0;padding:24px;background:#f5f5f5 }}
  h1 {{ color:#1a1a2e;margin-bottom:4px }}
  .meta {{ color:#888;font-size:13px;margin-bottom:24px }}
  .cards {{ display:flex;gap:12px;flex-wrap:wrap;margin-bottom:32px }}
  .card {{ background:#fff;border-radius:8px;padding:16px 20px;box-shadow:0 1px 4px rgba(0,0,0,.1);min-width:130px;text-align:center }}
  .num {{ font-size:32px;font-weight:700 }}
  .green {{ color:#16a34a }} .amber {{ color:#d97706 }} .red {{ color:#dc2626 }} .blue {{ color:#2563eb }}
  table {{ border-collapse:collapse;width:100%;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.1);margin-bottom:32px }}
  th {{ background:#1a1a2e;color:#fff;padding:10px 12px;text-align:left;font-size:12px;text-transform:uppercase }}
  td {{ padding:8px 12px;border-bottom:1px solid #f0f0f0;font-size:13px;vertical-align:top }}
  tr:last-child td {{ border-bottom:none }}
  tr:hover td {{ background:#f9fafb }}
  h2 {{ color:#1a1a2e;font-size:16px;margin-top:32px }}
  .query {{ background:#fff;border-radius:8px;padding:12px 16px;font-family:monospace;font-size:13px;color:#444;margin-bottom:24px;box-shadow:0 1px 4px rgba(0,0,0,.1) }}
</style>
</head>
<body>
<h1>📊 Weekly Universe Refresh</h1>
<p class="meta">Run: {results['run_at']} &nbsp;|&nbsp; {results['total_in_screen']} companies passed the screen (all NSE + BSE)</p>

<div class="query">🔍 Screen query: <strong>{results['query']}</strong></div>

<div class="cards">
  <div class="card"><div class="num green">{len(results['confirmed'])}</div>✅ Confirmed</div>
  <div class="card"><div class="num red">{len(results['degraded'])}</div>⚠️ Degraded</div>
  <div class="card"><div class="num blue">{len(results['strong_new'])}</div>🆕 New (strong)</div>
  <div class="card"><div class="num amber">{len(results['watch_new'])}</div>👀 Watch list</div>
  <div class="card"><div class="num">{results['total_in_screen']}</div>Total in screen</div>
</div>

{'<h2>⚠️ Degraded — no longer pass the screen (consider removing)</h2><table><tr><th>Ticker</th><th>Company</th><th>Exchange</th><th>Sector</th><th>Notes</th></tr>' + degraded_rows + '</table>' if results['degraded'] else ''}

{'<h2>🆕 New Strong Candidates — pass screen, not in universe (market cap > ₹' + f"{MIN_STRONG_CANDIDATE_MCAP:.0f}" + 'Cr)</h2><table><tr><th>Ticker</th><th>Company</th><th>Mkt Cap</th><th>ROCE</th><th>CMP</th><th>P/E</th></tr>' + strong_rows + '</table>' if results['strong_new'] else '<p style="color:#888">No new strong candidates this week.</p>'}

{'<h2>👀 Watch List — pass screen, market cap < ₹' + f"{MIN_STRONG_CANDIDATE_MCAP:.0f}" + 'Cr</h2><table><tr><th>Ticker</th><th>Company</th><th>Mkt Cap</th><th>ROCE</th></tr>' + watch_rows + '</table>' if results['watch_new'] else ''}

<h2>✅ Confirmed — existing universe, still in screen</h2>
<table>
<tr><th>Ticker</th><th>Company</th><th>Exchange</th><th>Sector</th><th>Mkt Cap</th><th>ROCE</th></tr>
{confirmed_rows}
</table>

<p style="color:#aaa;font-size:11px">AI Trading System · universe/universe_refresher.py · Not investment advice.</p>
</body></html>"""


# ---------------------------------------------------------------------------
# Email delivery
# ---------------------------------------------------------------------------

def send_refresh_report(results: dict) -> bool:
    """Email the HTML report via Resend (same mailer as trade alerts)."""
    from alerts.gmail_alert import _send_email

    n_new    = len(results["strong_new"])
    n_drop   = len(results["degraded"])
    subject  = (
        f"📊 Universe Refresh — {n_new} new candidates"
        + (f", {n_drop} degraded" if n_drop else "")
        + f" | {results['run_at']}"
    )
    html = format_html_report(results)
    plain = format_text_report(results)
    return _send_email(subject, html, plain_body=plain)


# ---------------------------------------------------------------------------
# Convenience runner (called by scheduler + webhook endpoint)
# ---------------------------------------------------------------------------

def run_and_email(screener_client=None) -> dict:
    """
    Full pipeline: run refresh → email report → return results dict.
    Safe to call from the scheduler — catches all exceptions and emails a failure notice.
    """
    from alerts.gmail_alert import _send_email

    try:
        results = run_universe_refresh(screener_client)
        send_refresh_report(results)
        log.info("Universe refresh complete — report emailed.")
        return results
    except Exception as e:
        log.error(f"Universe refresh failed: {e}", exc_info=True)
        try:
            _send_email(
                "❌ Universe refresh failed",
                f"<pre>{e}</pre>",
                plain_body=f"Universe refresh failed:\n{e}",
            )
        except Exception:
            pass
        raise


# ---------------------------------------------------------------------------
# CLI runner for local testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        results = run_universe_refresh()
        print(format_text_report(results))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
