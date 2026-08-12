"""
universe/diagnose_universe.py
-----------------------------
Compares the universe CSV against Kite's live NSE and BSE instrument lists.

Prints a report of:
  - Stocks that match NSE exactly (will be traded)
  - Stocks not in NSE but found in BSE (need exchange switch or different symbol)
  - Stocks with possible NSE symbol suggestions (fuzzy prefix match)
  - Stocks not found anywhere (need to remove or fix)

Run locally:
    KITE_API_KEY=xxx KITE_ACCESS_TOKEN=yyy python universe/diagnose_universe.py

Or via Flask endpoint: GET /diagnose_universe
"""

import csv
import os
import sys


def run_diagnosis(kite=None):
    """
    Run the diagnosis. Accepts an authenticated KiteConnect instance.
    If none provided, creates one from env vars.
    Returns a dict with results.
    """
    if kite is None:
        from kiteconnect import KiteConnect
        api_key = os.getenv("KITE_API_KEY")
        access_token = os.getenv("KITE_ACCESS_TOKEN")
        if not api_key or not access_token:
            raise ValueError("KITE_API_KEY and KITE_ACCESS_TOKEN must be set")
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)

    # Load universe CSV
    csv_path = os.path.join(os.path.dirname(__file__), "universe.csv")
    universe = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["Ticker"].strip():
                universe.append({
                    "ticker": row["Ticker"].strip(),
                    "company": row["Company"].strip(),
                    "notes": row.get("Notes", "").strip(),
                })

    print(f"Universe: {len(universe)} stocks")
    print("Fetching NSE instruments...")

    # Fetch NSE EQ instruments
    nse_instruments = kite.instruments("NSE")
    nse_eq = {
        r["tradingsymbol"]: r
        for r in nse_instruments
        if r["instrument_type"] == "EQ"
    }

    print(f"NSE EQ instruments: {len(nse_eq)}")
    print("Fetching BSE instruments...")

    # Fetch BSE EQ instruments
    bse_instruments = kite.instruments("BSE")
    bse_eq = {
        r["tradingsymbol"]: r
        for r in bse_instruments
        if r["instrument_type"] == "EQ"
    }

    print(f"BSE EQ instruments: {len(bse_eq)}")

    # Build a name-to-symbol lookup for fuzzy matching (name contains ticker substring)
    nse_names = {r["name"].upper(): r["tradingsymbol"] for r in nse_instruments if r["instrument_type"] == "EQ"}

    results = {
        "matched_nse": [],
        "found_bse_only": [],
        "suggested_nse": [],
        "not_found": [],
        "total": len(universe),
    }

    for stock in universe:
        ticker = stock["ticker"]
        company = stock["company"]

        if ticker in nse_eq:
            info = nse_eq[ticker]
            results["matched_nse"].append({
                "ticker": ticker,
                "company": company,
                "name": info["name"],
                "instrument_token": info["instrument_token"],
            })
            continue

        # Not in NSE — check BSE
        if ticker in bse_eq:
            bse_info = bse_eq[ticker]
            results["found_bse_only"].append({
                "ticker": ticker,
                "company": company,
                "bse_name": bse_info["name"],
                "bse_token": bse_info["instrument_token"],
                "note": "Listed on BSE; code fetches NSE only. Switch to BSE or find NSE symbol.",
            })
            continue

        # Not in either — try prefix/substring match on NSE names
        suggestions = []
        ticker_upper = ticker.upper()
        for sym, row in nse_eq.items():
            # symbol starts with first 5 chars of our ticker
            if sym.startswith(ticker_upper[:5]):
                suggestions.append(sym)
            # our ticker starts with first 5 chars of symbol
            elif ticker_upper.startswith(sym[:5]) and len(sym) >= 5:
                suggestions.append(sym)

        # Also check company name similarity
        company_upper = company.upper()
        for name_key, sym in nse_names.items():
            if company_upper[:8] in name_key or name_key[:8] in company_upper:
                if sym not in suggestions:
                    suggestions.append(sym)

        if suggestions:
            results["suggested_nse"].append({
                "ticker": ticker,
                "company": company,
                "suggestions": suggestions[:5],  # top 5
                "note": stock["notes"][:80] if stock["notes"] else "",
            })
        else:
            results["not_found"].append({
                "ticker": ticker,
                "company": company,
                "note": stock["notes"][:80] if stock["notes"] else "",
            })

    return results


def format_text_report(results):
    lines = []
    lines.append("=" * 70)
    lines.append("UNIVERSE DIAGNOSTIC REPORT")
    lines.append("=" * 70)
    lines.append(f"Total stocks in universe CSV: {results['total']}")
    lines.append(f"  ✅ Matched in NSE (will trade):      {len(results['matched_nse'])}")
    lines.append(f"  ⚠️  Found in BSE only:                {len(results['found_bse_only'])}")
    lines.append(f"  🔍 Not found, but NSE suggestions:   {len(results['suggested_nse'])}")
    lines.append(f"  ❌ Not found anywhere:                {len(results['not_found'])}")
    lines.append("")

    lines.append("─" * 70)
    lines.append("✅ MATCHED IN NSE (actively evaluated each cycle)")
    lines.append("─" * 70)
    for s in results["matched_nse"]:
        lines.append(f"  {s['ticker']:<20} {s['name']}")
    lines.append("")

    lines.append("─" * 70)
    lines.append("⚠️  FOUND IN BSE ONLY (code fetches NSE — these are always skipped)")
    lines.append("─" * 70)
    for s in results["found_bse_only"]:
        lines.append(f"  {s['ticker']:<20} BSE name: {s['bse_name']}")
        lines.append(f"    → {s['note']}")
    lines.append("")

    lines.append("─" * 70)
    lines.append("🔍 NOT IN NSE OR BSE — possible symbol corrections")
    lines.append("─" * 70)
    for s in results["suggested_nse"]:
        lines.append(f"  {s['ticker']:<20} ({s['company']})")
        lines.append(f"    Possible NSE symbols: {', '.join(s['suggestions'])}")
        if s["note"]:
            lines.append(f"    Note: {s['note']}")
    lines.append("")

    lines.append("─" * 70)
    lines.append("❌ NOT FOUND ANYWHERE — remove or fix these")
    lines.append("─" * 70)
    for s in results["not_found"]:
        lines.append(f"  {s['ticker']:<20} ({s['company']})")
        if s["note"]:
            lines.append(f"    Note: {s['note']}")
    lines.append("")
    lines.append("=" * 70)
    return "\n".join(lines)


def format_html_report(results):
    def rows(items, cols):
        out = ""
        for item in items:
            out += "<tr>" + "".join(f"<td>{item.get(c,'')}</td>" for c in cols) + "</tr>\n"
        return out

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Universe Diagnostic</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 32px; background: #f8f9fa; }}
  h1 {{ color: #1a1a2e; }}
  .summary {{ display: flex; gap: 16px; flex-wrap: wrap; margin: 20px 0; }}
  .card {{ background: #fff; border-radius: 8px; padding: 16px 24px; box-shadow: 0 1px 4px rgba(0,0,0,.1); min-width: 160px; text-align: center; }}
  .card .num {{ font-size: 36px; font-weight: 700; }}
  .green {{ color: #16a34a; }} .amber {{ color: #d97706; }} .red {{ color: #dc2626; }} .blue {{ color: #2563eb; }}
  table {{ border-collapse: collapse; width: 100%; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,.1); margin-bottom: 32px; }}
  th {{ background: #1a1a2e; color: #fff; padding: 10px 14px; text-align: left; font-size: 13px; }}
  td {{ padding: 8px 14px; border-bottom: 1px solid #f0f0f0; font-size: 13px; vertical-align: top; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: #f9fafb; }}
  h2 {{ margin-top: 32px; color: #1a1a2e; font-size: 16px; }}
</style>
</head>
<body>
<h1>🔍 Universe Diagnostic Report</h1>

<div class="summary">
  <div class="card"><div class="num">{results['total']}</div>Total stocks</div>
  <div class="card"><div class="num green">{len(results['matched_nse'])}</div>✅ NSE matched</div>
  <div class="card"><div class="num amber">{len(results['found_bse_only'])}</div>⚠️ BSE only</div>
  <div class="card"><div class="num blue">{len(results['suggested_nse'])}</div>🔍 Has suggestions</div>
  <div class="card"><div class="num red">{len(results['not_found'])}</div>❌ Not found</div>
</div>

<h2>✅ Matched in NSE — actively evaluated each cycle</h2>
<table>
<tr><th>Ticker</th><th>Kite Name</th><th>Token</th></tr>
{''.join(f"<tr><td><b>{s['ticker']}</b></td><td>{s['name']}</td><td>{s['instrument_token']}</td></tr>" for s in results['matched_nse'])}
</table>

<h2>⚠️ Found in BSE only — skipped every cycle (code only checks NSE)</h2>
<table>
<tr><th>CSV Ticker</th><th>Company</th><th>BSE Name</th><th>Action needed</th></tr>
{''.join(f"<tr><td><b>{s['ticker']}</b></td><td>{s['company']}</td><td>{s['bse_name']}</td><td>Add BSE exchange support or find NSE symbol</td></tr>" for s in results['found_bse_only'])}
</table>

<h2>🔍 Symbol not in NSE or BSE — possible corrections</h2>
<table>
<tr><th>CSV Ticker</th><th>Company</th><th>Possible NSE Symbols</th><th>Notes</th></tr>
{''.join(f"<tr><td><b>{s['ticker']}</b></td><td>{s['company']}</td><td>{', '.join(s['suggestions'])}</td><td>{s['note']}</td></tr>" for s in results['suggested_nse'])}
</table>

<h2>❌ Not found anywhere — remove or fix</h2>
<table>
<tr><th>CSV Ticker</th><th>Company</th><th>Notes</th></tr>
{''.join(f"<tr><td><b>{s['ticker']}</b></td><td>{s['company']}</td><td>{s['note']}</td></tr>" for s in results['not_found'])}
</table>

<p style="color:#888;font-size:12px;">Generated by AI Trading System · universe/diagnose_universe.py</p>
</body></html>"""
    return html


if __name__ == "__main__":
    try:
        results = run_diagnosis()
        print(format_text_report(results))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
