"""
Exit study — does a wider trailing stop, or a split exit, keep more of the
big moves without giving it all back on everything else?

Same universe (universe/universe.csv, 645 stocks), same live entries
(52wk_breakout OR bb_squeeze_break OR golden_cross_ema), same -7% hard stop
from entry, same tier costs. Only the exit changes:

  T20    20% trailing stop (what runs live today)
  T30    30% trailing stop
  T40    40% trailing stop
  SPLIT  half the position on a 20% trail, half on a 40% trail; no re-entry
         until both halves are out. Cost charged once per round trip.

Note how the hard stop interacts: exit = HIGHER of entry*0.93 and the trail
line, so a 40% trail only takes over once the trade is up ~55% (30%: ~33%).
Until then every variant is the same -7% stop — the variants differ only in
how long they sit through a pullback AFTER a trade has worked.

Reported per variant and tier:
  trade-level  trades, win%, avg return/trade, profit factor, avg days held
  per-stock    engine compounded on each stock alone: median multiple,
               % of stocks it lost money on
  big movers   stocks up >=5x over 10y buy-and-hold: median share of the
               move kept (engine multiple / buy-and-hold multiple)

Data hygiene: Yahoo occasionally carries an unadjusted split/bonus as a
one-day crash. Stocks with any one-day move beyond +80% / -45% are listed
and EXCLUDED from the aggregates (the LUMAXTECH 0.08x result last run).

Output: streak_backtests/results/exit_study.txt and exit_study.csv
"""

import csv
import os
import sys
from statistics import median

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest import STRATEGIES, build_ctx, fetch_history  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "streak_backtests", "results")
LIVE = ["52wk_breakout", "bb_squeeze_break", "golden_cross_ema"]
COST = {"Midcap150": 0.5, "Smallcap250": 0.8, "Microcap250": 1.2}
HARD_STOP = 7.0
TIERS = ("Midcap150", "Smallcap250", "Microcap250")
VARIANTS = {"T20": (20, 20), "T30": (30, 30), "T40": (40, 40), "SPLIT": (20, 40)}


def sim(C, H, L, entries, trail_a, trail_b, cost):
    """Two half-positions with their own trails (equal trails == one position).
    Returns [(ret_pct, bars_held)] per round trip, full-position basis."""
    out, i, n = [], 0, len(C)
    while i < n:
        if not entries[i]:
            i += 1
            continue
        entry = C[i]
        hard = entry * (1 - HARD_STOP / 100)
        peak = entry
        open_ = {"a": trail_a, "b": trail_b}
        exits = {}
        j = i + 1
        while j < n and open_:
            peak = max(peak, H[j])
            for leg, tr in list(open_.items()):
                line = max(hard, peak * (1 - tr / 100))
                if L[j] <= line:
                    exits[leg] = (line, j)
                    del open_[leg]
            j += 1
        for leg in list(open_):            # still open at the end of data
            exits[leg] = (C[-1], n - 1)
        ret = sum((px - entry) / entry * 100 for px, _ in exits.values()) / 2 - cost
        last = max(b for _, b in exits.values())
        out.append((ret, last - i))
        i = last + 1
    return out


def bad_data(C):
    return any(C[k - 1] > 0 and (C[k] / C[k - 1] > 1.8 or C[k] / C[k - 1] < 0.55)
               for k in range(1, len(C)))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    with open(os.path.join(ROOT, "universe", "universe.csv"), newline="", encoding="utf-8") as f:
        uni = [(r["Ticker"], r.get("Notes", "")) for r in csv.DictReader(f)]

    trades = {(v, t): [] for v in VARIANTS for t in TIERS}
    per_stock, flagged = [], []
    for k, (tk, tier) in enumerate(uni, 1):
        hist = fetch_history(tk, "NSE", 10.2)
        if len(hist) < 260:
            continue
        C = [b["close"] for b in hist]; H = [b["high"] for b in hist]; L = [b["low"] for b in hist]
        V = [b["volume"] for b in hist]
        if bad_data(C):
            flagged.append(tk); print(f"[{k}] {tk}: suspicious one-day jump — excluded"); continue
        ctx = build_ctx(H, L, C, V)
        entries = [any(STRATEGIES[s](ctx, i) for s in LIVE) for i in range(len(C))]
        n10 = 2480
        bh10 = C[-1] / C[-n10] if len(C) >= n10 * 0.95 and C[-min(n10, len(C))] > 0 else None
        row = {"ticker": tk, "tier": tier, "bh_10y": round(bh10, 2) if bh10 else ""}
        for v, (ta, tb) in VARIANTS.items():
            tr = sim(C, H, L, entries, ta, tb, COST.get(tier, 1.0))
            if tier in TIERS:
                trades[(v, tier)] += tr
            g = 1.0
            for r, _ in tr:
                g *= 1 + r / 100
            row[v] = round(g, 3)
        per_stock.append(row)
        print(f"[{k}/{len(uni)}] {tk}: " + "  ".join(f"{v} x{row[v]}" for v in VARIANTS))

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "exit_study.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(per_stock[0].keys())); w.writeheader(); w.writerows(per_stock)

    out = []; p = out.append
    p("EXIT STUDY — same entries, different exits (20/30/40% trail, split 20+40)")
    p("=" * 86)
    for tier in TIERS + ("ALL",):
        tiers = TIERS if tier == "ALL" else (tier,)
        p(f"\n{tier}")
        p(f"  {'exit':<6}{'trades':>7}{'win%':>7}{'avg/trade':>11}{'PF':>7}{'days held':>11}"
          f"{'stock median':>14}{'stocks lost':>13}{'kept of 5x+':>13}")
        rows = [r for r in per_stock if r["tier"] in tiers]
        big = [r for r in rows if r["bh_10y"] != "" and r["bh_10y"] >= 5]
        for v in VARIANTS:
            tr = [t for tt in tiers for t in trades[(v, tt)]]
            if not tr:
                continue
            rets = [r for r, _ in tr]
            wins = [r for r in rets if r > 0]; losses = [r for r in rets if r <= 0]
            pf = sum(wins) / abs(sum(losses)) if losses and sum(losses) else float("inf")
            sm = median(r[v] for r in rows)
            lost = sum(r[v] < 1 for r in rows) / len(rows) * 100
            keep = median(r[v] / r["bh_10y"] for r in big) if big else float("nan")
            p(f"  {v:<6}{len(tr):>7}{len(wins)/len(tr)*100:>7.1f}{sum(rets)/len(rets):>10.2f}%"
              f"{pf:>7.2f}{median(b for _, b in tr):>11.0f}{sm:>13.2f}x{lost:>12.0f}%{keep:>12.2f}")
        p(f"  ({len(rows)} stocks; {len(big)} were >=5x over 10y buy-and-hold)")

    big = sorted([r for r in per_stock if r["bh_10y"] != "" and r["bh_10y"] >= 10],
                 key=lambda r: -r["bh_10y"])
    p(f"\n10y >=10x stocks — engine multiple per exit (buy-and-hold for reference)")
    p(f"  {'ticker':<12}{'tier':<13}{'B&H':>8}" + "".join(f"{v:>9}" for v in VARIANTS))
    for r in big[:30]:
        p(f"  {r['ticker']:<12}{r['tier']:<13}{r['bh_10y']:>7.1f}x" + "".join(f"{r[v]:>8.2f}x" for v in VARIANTS))
    p(f"\nExcluded for suspicious one-day price jumps ({len(flagged)}): {', '.join(flagged) or '—'}")
    p("Caveats: today's members only (survivorship flatters every variant equally);")
    p("each stock traded alone — wider trails also hold positions longer, which in the")
    p("real portfolio means fewer free slots for new signals (see 'days held').")
    txt = "\n".join(out)
    with open(os.path.join(OUT_DIR, "exit_study.txt"), "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    print("\n" + txt)


if __name__ == "__main__":
    main()
