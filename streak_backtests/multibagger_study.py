"""
Multibagger study — answers "did the micro/small/mid caps actually produce
multibaggers, and how much of those moves would the live engine have kept?"

For every stock in universe/universe.csv (tier in the Notes column):
  * buy-and-hold multiple over the last 5y and 10y (split/bonus-adjusted)
  * best trough-to-peak run inside the window
  * rough market cap 10y / 5y ago = market cap today / price multiple
    (ignores new share issuance, so it slightly OVERstates the old cap)
  * what the live engine would have compounded trading only that stock:
    entries = 52wk_breakout OR bb_squeeze_break OR golden_cross_ema,
    one position at a time, -7% hard stop, 20% trailing stop, tier costs.

Caveat that matters most: this uses TODAY's index members. The decade's
real microcap multibaggers mostly GRADUATED into Smallcap/Midcap and so
show up under those tiers here, with a small "est. cap 10y ago". Read the
"was small 10y ago" section, not the tier column, for the multibagger
question.

Output: streak_backtests/results/multibagger_study.txt and .csv
"""

import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest import (STRATEGIES, build_ctx, fetch_history, simulate)  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "streak_backtests", "results")
LIVE = ["52wk_breakout", "bb_squeeze_break", "golden_cross_ema"]
COST = {"Midcap150": 0.5, "Smallcap250": 0.8, "Microcap250": 1.2}
BARS_PER_YEAR = 248
SMALL_THEN_CR = 1000    # "was a micro/nano-cap" if est. cap then < ₹1,000 Cr


def mcap_cr(ticker):
    try:
        import yfinance as yf
        v = yf.Ticker(ticker + ".NS").fast_info["market_cap"]
        return float(v) / 1e7 if v else None
    except Exception:
        return None


def window_multiple(C, years):
    n = int(years * BARS_PER_YEAR)
    if len(C) < n * 0.95:
        return None
    start = C[-n] if len(C) >= n else C[0]
    return C[-1] / start if start > 0 else None


def best_run(C):
    lo, best = C[0], 1.0
    for c in C:
        lo = min(lo, c)
        if lo > 0:
            best = max(best, c / lo)
    return best


def engine_compound(C, H, L, V, cost):
    ctx = build_ctx(H, L, C, V)
    entries = [any(STRATEGIES[s](ctx, i) for s in LIVE) for i in range(len(C))]
    trades = simulate(C, H, L, entries, "trail", 7.0, 20.0, 45, cost_pct=cost)
    g = 1.0
    for t in trades:
        g *= 1 + t.ret_pct / 100
    return g, len(trades)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows console is cp1252; the report has ₹
    except Exception:
        pass
    rows = []
    with open(os.path.join(ROOT, "universe", "universe.csv"), newline="", encoding="utf-8") as f:
        uni = [(r["Ticker"], r.get("Notes", "")) for r in csv.DictReader(f)]
    for k, (t, tier) in enumerate(uni, 1):
        hist = fetch_history(t, "NSE", 10.2)
        if len(hist) < 260:
            print(f"[{k}/{len(uni)}] {t}: no/thin history"); continue
        C = [b["close"] for b in hist]; H = [b["high"] for b in hist]
        L = [b["low"] for b in hist]; V = [b["volume"] for b in hist]
        m10, m5 = window_multiple(C, 10), window_multiple(C, 5)
        yrs = len(C) / BARS_PER_YEAR
        full = C[-1] / C[0] if C[0] > 0 else None
        eng, ntr = engine_compound(C, H, L, V, COST.get(tier, 1.0))
        cap = mcap_cr(t)
        rows.append(dict(
            ticker=t, tier=tier, years=round(yrs, 1), mcap_now_cr=round(cap) if cap else "",
            mult_10y=round(m10, 2) if m10 else "", mult_5y=round(m5, 2) if m5 else "",
            mult_full=round(full, 2) if full else "", best_run=round(best_run(C), 2),
            est_cap_10y_ago=round(cap / m10) if cap and m10 else "",
            est_cap_5y_ago=round(cap / m5) if cap and m5 else "",
            engine_mult=round(eng, 2), engine_trades=ntr))
        print(f"[{k}/{len(uni)}] {t}: 10y x{m10 and round(m10,1)}  engine x{round(eng,2)}")

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "multibagger_study.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)

    out = []
    p = out.append
    p("MULTIBAGGER STUDY — today's Midcap150 / Smallcap250 / Microcap250 members")
    p("=" * 78)
    for label, key in (("10-year", "mult_10y"), ("5-year", "mult_5y")):
        p(f"\n{label} buy-and-hold multiples (stocks with full {label} history)")
        p(f"{'tier':<13}{'n':>5}{'>=3x':>7}{'>=5x':>7}{'>=10x':>7}{'<1x (lost)':>12}{'median':>9}")
        for tier in ("Midcap150", "Smallcap250", "Microcap250"):
            xs = sorted(r[key] for r in rows if r["tier"] == tier and r[key] != "")
            if not xs:
                continue
            med = xs[len(xs) // 2]
            p(f"{tier:<13}{len(xs):>5}{sum(x >= 3 for x in xs):>7}{sum(x >= 5 for x in xs):>7}"
              f"{sum(x >= 10 for x in xs):>7}{sum(x < 1 for x in xs):>12}{med:>9.2f}")

    for key, lab in (("est_cap_10y_ago", "10y"), ("est_cap_5y_ago", "5y")):
        mk = "mult_10y" if lab == "10y" else "mult_5y"
        small = [r for r in rows if r[key] != "" and r[key] < SMALL_THEN_CR]
        mb = sorted([r for r in small if r[mk] >= 5], key=lambda r: -r[mk])
        p(f"\nWas < ₹{SMALL_THEN_CR:,} Cr ~{lab} ago (est.): {len(small)} stocks; "
          f"{len(mb)} of them went >=5x, {sum(r[mk] >= 10 for r in small)} went >=10x")
        p(f"  where those >=5x sit today: " + ", ".join(
            f"{t}={sum(r['tier'] == t for r in mb)}" for t in ("Midcap150", "Smallcap250", "Microcap250")))
        if lab == "10y":
            p(f"  {'ticker':<12}{'tier now':<13}{'cap then':>9}{'cap now':>9}{'B&H':>8}{'engine':>8}{'trades':>7}")
            for r in mb[:40]:
                p(f"  {r['ticker']:<12}{r['tier']:<13}{r[key]:>9}{r['mcap_now_cr']:>9}"
                  f"{r[mk]:>7.1f}x{r['engine_mult']:>7.2f}x{r['engine_trades']:>7}")

    mbs = [r for r in rows if r["mult_10y"] != "" and r["mult_10y"] >= 5]
    if mbs:
        cap = sorted(r["engine_mult"] / r["mult_10y"] for r in mbs)
        p(f"\nEngine capture on all {len(mbs)} 10y >=5x stocks: median engine/B&H = "
          f"{cap[len(cap)//2]:.2f} (1.00 = kept the whole move)")
        p(f"  engine turned them into >=5x itself: {sum(r['engine_mult'] >= 5 for r in mbs)}, "
          f">=2x: {sum(r['engine_mult'] >= 2 for r in mbs)}, lost money: {sum(r['engine_mult'] < 1 for r in mbs)}")
    for tier in ("Midcap150", "Smallcap250", "Microcap250"):
        xs = sorted(r["engine_mult"] for r in rows if r["tier"] == tier)
        if xs:
            p(f"Engine per-stock compounded result, {tier}: median x{xs[len(xs)//2]:.2f}, "
              f"lost money on {sum(x < 1 for x in xs)}/{len(xs)}")
    p("\nCaveats: today's members only (survivorship); est. caps ignore dilution;")
    p("engine figures trade one stock in isolation (no portfolio, no slot competition).")
    txt = "\n".join(out)
    with open(os.path.join(OUT_DIR, "multibagger_study.txt"), "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    print("\n" + txt)


if __name__ == "__main__":
    main()
