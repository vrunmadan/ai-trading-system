"""
Local strategy/regime backtester — no Streak, no LLM, no Kite token.

Replays 10 years of DAILY history bar-by-bar over a universe, tests a library of
candidate entry strategies with configurable exits, and (optionally) breaks the
results down BY MARKET REGIME so you can see which strategy belongs in which
regime basket. Data is free Yahoo Finance, so no Kite login / Railway needed.

One-time setup:
    pip install yfinance

Run (exit comparison, all strategies, one universe):
    python streak_backtests/backtest.py --universe streak_backtests/nifty100.csv --trail 20

Run (REGIME basket view — which strategy wins in bull/sideways/bear/etc.):
    python streak_backtests/backtest.py --universe streak_backtests/nifty100.csv --trail 20 --by-regime

Flags: --years 10  --stop 7  --trail 20  --time-exit 45  --target 20  --by-regime

Caveats (unchanged, important): uses TODAY's index members (survivorship bias
inflates results), no transaction costs/slippage, and the regime tag uses Nifty
vs its 200-EMA + India VIX (breadth omitted vs the live classifier). Trust the
RELATIVE picture (which strategy/regime/exit beats which), not absolute P&L.
"""

import argparse
import csv
import os
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta

# ===========================================================================
# Indicator series (precomputed once per stock — arrays aligned to bars)
# ===========================================================================

def rsi_series(C, period=14):
    n = len(C); out = [50.0] * n
    if n < period + 1:
        return out
    gains = [max(C[i] - C[i - 1], 0.0) for i in range(1, n)]
    losses = [max(C[i - 1] - C[i], 0.0) for i in range(1, n)]
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    def rv(ag, al):
        if al == 0: return 100.0
        rs = ag / al; return 100 - 100 / (1 + rs)
    out[period] = rv(ag, al)
    for k in range(period, n - 1):
        ag = (ag * (period - 1) + gains[k]) / period
        al = (al * (period - 1) + losses[k]) / period
        out[k + 1] = rv(ag, al)
    return out


def supertrend_series(H, L, C, period=10, multiplier=3.0):
    n = len(C)
    if n < period + 3:
        return ["UNKNOWN"] * n
    tr = [0.0]
    for i in range(1, n):
        tr.append(max(H[i] - L[i], abs(H[i] - C[i - 1]), abs(L[i] - C[i - 1])))
    atr = [0.0] * n
    atr[period] = sum(tr[1:period + 1]) / period
    for i in range(period + 1, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    directions = ["UNKNOWN"] * n
    prev_dir, prev_up, prev_lo = "GREEN", None, None
    for i in range(period, n):
        mid = (H[i] + L[i]) / 2
        up = mid + multiplier * atr[i]; lo = mid - multiplier * atr[i]
        if prev_up is not None:
            up = min(up, prev_up) if C[i - 1] < prev_up else up
            lo = max(lo, prev_lo) if C[i - 1] > prev_lo else lo
        if prev_dir == "RED" and C[i] > up: cur = "GREEN"
        elif prev_dir == "GREEN" and C[i] < lo: cur = "RED"
        else: cur = prev_dir
        directions[i] = cur
        prev_dir, prev_up, prev_lo = cur, up, lo
    return directions


def ema_series(C, period):
    n = len(C)
    if n == 0: return []
    k = 2.0 / (period + 1)
    out = [C[0]]; ema = C[0]
    for i in range(1, n):
        ema = C[i] * k + ema * (1 - k); out.append(ema)
    return out


def sma_series(C, period):
    n = len(C); out = [0.0] * n; s = 0.0
    for i in range(n):
        s += C[i]
        if i >= period: s -= C[i - period]
        out[i] = s / min(i + 1, period)
    return out


def adx_series(H, L, C, period=14):
    n = len(C); pdi = [0.0] * n; mdi = [0.0] * n; adx = [0.0] * n
    if n < 2 * period + 2:
        return pdi, mdi, adx
    tr = [0.0] * n; pdm = [0.0] * n; mdm = [0.0] * n
    for i in range(1, n):
        up = H[i] - H[i - 1]; dn = L[i - 1] - L[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        mdm[i] = dn if (dn > up and dn > 0) else 0.0
        tr[i] = max(H[i] - L[i], abs(H[i] - C[i - 1]), abs(L[i] - C[i - 1]))
    atr = sum(tr[1:period + 1]); sp = sum(pdm[1:period + 1]); sm = sum(mdm[1:period + 1])
    dxs = []
    for i in range(period + 1, n):
        atr = atr - atr / period + tr[i]
        sp = sp - sp / period + pdm[i]
        sm = sm - sm / period + mdm[i]
        p = 100 * sp / atr if atr else 0.0
        m = 100 * sm / atr if atr else 0.0
        pdi[i] = p; mdi[i] = m
        denom = p + m
        dxs.append((i, 100 * abs(p - m) / denom if denom else 0.0))
    if len(dxs) >= period:
        av = sum(d for _, d in dxs[:period]) / period
        adx[dxs[period - 1][0]] = av
        for k in range(period, len(dxs)):
            i, dx = dxs[k]; av = (av * (period - 1) + dx) / period; adx[i] = av
    return pdi, mdi, adx


def cci_series(H, L, C, period=20):
    n = len(C); out = [0.0] * n
    tp = [(H[i] + L[i] + C[i]) / 3 for i in range(n)]
    for i in range(period - 1, n):
        w = tp[i - period + 1:i + 1]; m = sum(w) / period
        md = sum(abs(x - m) for x in w) / period
        out[i] = (tp[i] - m) / (0.015 * md) if md else 0.0
    return out


def bb_series(C, period=20, mult=2.0):
    n = len(C); up = [0.0] * n; lo = [0.0] * n; pos = [50.0] * n; bw = [0.0] * n
    for i in range(period - 1, n):
        w = C[i - period + 1:i + 1]; m = sum(w) / period
        std = (sum((x - m) ** 2 for x in w) / period) ** 0.5
        u = m + mult * std; l = m - mult * std
        up[i], lo[i] = u, l
        pos[i] = (C[i] - l) / (u - l) * 100 if u != l else 50.0
        bw[i] = (u - l) / m * 100 if m else 0.0
    return up, lo, pos, bw


def build_ctx(H, L, C, V):
    n = len(C)
    up, lo, pos, bw = bb_series(C)
    pdi, mdi, adx = adx_series(H, L, C)
    hi52 = [max(C[max(0, i - 251):i + 1]) for i in range(n)]
    lo52 = [min(L[max(0, i - 251):i + 1]) for i in range(n)]
    volr = [0.0] * n
    for i in range(1, n):
        w = V[max(0, i - 20):i]; a = sum(w) / len(w) if w else 0.0
        volr[i] = V[i] / a if a > 0 else 0.0
    return dict(H=H, L=L, C=C, V=V, n=n,
                rsi=rsi_series(C), st=supertrend_series(H, L, C),
                sma50=sma_series(C, 50), ema50=ema_series(C, 50), ema200=ema_series(C, 200),
                pdi=pdi, mdi=mdi, adx=adx, cci=cci_series(H, L, C),
                bb_up=up, bb_lo=lo, bb_pos=pos, bb_bw=bw, hi52=hi52, lo52=lo52, volr=volr)


# ===========================================================================
# Strategy library — entry(ctx, i) -> bool.  (trend/breakout, then mean-reversion)
# ===========================================================================

def e_52wk_breakout(x, i):
    if i < 60: return False
    return (x["C"][i] >= 0.99 * x["hi52"][i] and x["volr"][i] >= 1.5
            and 50 <= x["rsi"][i] <= 70 and x["st"][i] == "GREEN")

def e_supertrend_buy(x, i):
    if i < 60 or x["st"][i] == "UNKNOWN" or x["st"][i - 1] == "UNKNOWN": return False
    return (x["st"][i] == "GREEN" and x["st"][i - 1] == "RED"
            and x["C"][i] > x["sma50"][i] and 40 <= x["rsi"][i] <= 65)

def e_golden_cross(x, i):
    if i < 205: return False
    return x["ema50"][i] > x["ema200"][i] and x["ema50"][i - 1] <= x["ema200"][i - 1]

def e_adx_bull(x, i):
    if i < 40: return False
    return (x["adx"][i] > 25 and x["adx"][i - 1] <= 25
            and x["pdi"][i] > x["mdi"][i] and x["C"][i] > x["sma50"][i])

def e_bb_squeeze_breakout(x, i):
    if i < 120: return False
    window = x["bb_bw"][i - 100:i]
    if not window: return False
    thresh = sorted(window)[int(0.20 * len(window))]
    squeeze = x["bb_bw"][i - 1] <= thresh and x["bb_bw"][i - 1] > 0
    breakout = x["C"][i] > x["bb_up"][i] and x["C"][i - 1] <= x["bb_up"][i - 1]
    return squeeze and breakout and x["rsi"][i] > 50

def e_bb_mean_reversion(x, i):
    if i < 60: return False
    return (x["C"][i] < x["bb_lo"][i] and x["rsi"][i] < 40
            and x["C"][i] > 1.02 * x["lo52"][i])

def e_rsi_mean_reversion(x, i):
    if i < 60: return False
    return x["rsi"][i] < 35 and x["bb_pos"][i] < 25 and x["C"][i] > 1.02 * x["lo52"][i]

def e_cci_recovery(x, i):
    if i < 40: return False
    return (x["cci"][i] > -100 and x["cci"][i - 1] <= -100
            and x["C"][i] > 1.02 * x["lo52"][i])


STRATEGIES = {
    # trend / breakout
    "52wk_breakout":      e_52wk_breakout,
    "supertrend_buy":     e_supertrend_buy,
    "golden_cross_ema":   e_golden_cross,
    "adx_bull_strength":  e_adx_bull,
    "bb_squeeze_break":   e_bb_squeeze_breakout,
    # mean reversion
    "bb_mean_reversion":  e_bb_mean_reversion,
    "rsi_mean_reversion": e_rsi_mean_reversion,
    "cci_recovery":       e_cci_recovery,
}


# ===========================================================================
# Data + market regime
# ===========================================================================

def load_universe(path):
    out = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            t = (row.get("Ticker") or row.get("ticker") or "").strip()
            ex = (row.get("Exchange") or row.get("exchange") or "NSE").strip() or "NSE"
            if t: out.append((t, ex))
    return out


_YAHOO_SUFFIX = {"NSE": ".NS", "BSE": ".BO"}


def fetch_history(ticker, exchange, years):
    """Return list of {date, high, low, close, volume} daily bars, or []."""
    import yfinance as yf
    suffix = _YAHOO_SUFFIX.get(exchange.upper(), ".NS")
    start = (date.today() - timedelta(days=int(years * 365.25) + 5)).strftime("%Y-%m-%d")
    try:
        df = yf.download(ticker + suffix, start=start, interval="1d",
                         auto_adjust=True, progress=False)
    except Exception as e:
        print(f"    yfinance error for {ticker}{suffix}: {e}"); return []
    if df is None or len(df) == 0:
        return []
    cols = {str(c[0]).lower() if isinstance(c, tuple) else str(c).lower(): c for c in df.columns}
    try:
        H = df[cols["high"]].tolist(); L = df[cols["low"]].tolist()
        C = df[cols["close"]].tolist(); V = df[cols["volume"]].tolist()
    except KeyError:
        return []
    dts = [d.strftime("%Y-%m-%d") for d in df.index]
    bars = []
    for d, h, l, c, v in zip(dts, H, L, C, V):
        if None in (h, l, c) or c != c: continue
        bars.append({"date": d, "high": float(h), "low": float(l),
                     "close": float(c), "volume": float(v) if v == v else 0.0})
    return bars


def _classify(pct, vix):
    if vix >= 30 or pct <= -12: return "CRASH"
    if pct >= 8:  return "EUPHORIA"
    if pct >= 2:  return "BULL"
    if pct >= -3: return "SIDEWAYS"
    if pct >= -12: return "BEAR"
    return "CRASH"


def build_regime_by_date(years):
    """Nifty vs its 200-EMA + India VIX -> regime per date (dict date->regime)."""
    import yfinance as yf
    start = (date.today() - timedelta(days=int(years * 365.25) + 400)).strftime("%Y-%m-%d")
    def closes(sym):
        try:
            df = yf.download(sym, start=start, interval="1d", auto_adjust=True, progress=False)
        except Exception:
            return {}
        if df is None or len(df) == 0: return {}
        col = [c for c in df.columns if (str(c[0]).lower() if isinstance(c, tuple) else str(c).lower()) == "close"][0]
        return {d.strftime("%Y-%m-%d"): float(v) for d, v in zip(df.index, df[col].tolist()) if v == v}
    nifty = closes("^NSEI"); vix = closes("^INDIAVIX")
    if not nifty:
        return {}
    dts = sorted(nifty)
    cl = [nifty[d] for d in dts]
    ema = ema_series(cl, 200)
    out = {}
    for k, d in enumerate(dts):
        pct = (cl[k] - ema[k]) / ema[k] * 100 if ema[k] else 0.0
        out[d] = _classify(pct, vix.get(d, 18.0))
    return out


# ===========================================================================
# Simulation
# ===========================================================================

@dataclass
class Trade:
    ret_pct: float
    bars_held: int
    regime: str = ""


def simulate(C, H, L, entries, exit_variant, stop_pct, trail_pct, time_exit,
             target_pct=0.0, regime_arr=None, cost_pct=0.0):
    """cost_pct = round-trip transaction cost (brokerage+slippage+impact), in %,
    subtracted from every trade's return."""
    trades, i, n = [], 0, len(C)
    in_pos = False
    while i < n:
        if not in_pos and entries[i]:
            entry_px = C[i]; peak = entry_px; bars = 0; j = i + 1; in_pos = True
            reg = regime_arr[i] if regime_arr else ""
            while j < n:
                bars += 1; px = C[j]; peak = max(peak, H[j])
                stop_line = entry_px * (1 - stop_pct / 100)
                trail_line = peak * (1 - trail_pct / 100)
                target_line = entry_px * (1 + target_pct / 100) if target_pct > 0 else None
                exit_now, exit_px = False, px
                if L[j] <= stop_line:
                    exit_now, exit_px = True, stop_line
                elif exit_variant == "trail" and L[j] <= trail_line:
                    exit_now, exit_px = True, trail_line
                elif exit_variant == "time" and bars >= time_exit:
                    exit_now, exit_px = True, px
                elif exit_variant == "combo":
                    if L[j] <= trail_line: exit_now, exit_px = True, trail_line
                    elif target_line is not None and H[j] >= target_line: exit_now, exit_px = True, target_line
                    elif bars >= time_exit: exit_now, exit_px = True, px
                if exit_now:
                    trades.append(Trade((exit_px - entry_px) / entry_px * 100 - cost_pct, bars, reg))
                    in_pos = False; i = j; break
                j += 1
            else:
                trades.append(Trade((C[-1] - entry_px) / entry_px * 100 - cost_pct, bars, reg))
                in_pos = False; break
        i += 1
    return trades


POSITION_FRACTION = 0.20


def stats(trades):
    if not trades:
        return dict(n=0, win=None, avgwin=None, avgloss=None, expect=None, pf=None, mdd=None, tot=None)
    rets = [t.ret_pct for t in trades]
    wins = [r for r in rets if r > 0]; losses = [r for r in rets if r <= 0]
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")
    cum = peak = mdd = 0.0
    for r in rets:
        cum += POSITION_FRACTION * r; peak = max(peak, cum); mdd = min(mdd, cum - peak)
    return dict(n=len(trades), win=len(wins) / len(trades) * 100,
                avgwin=(sum(wins) / len(wins)) if wins else 0.0,
                avgloss=(sum(losses) / len(losses)) if losses else 0.0,
                expect=sum(rets) / len(rets), pf=pf, mdd=mdd, tot=cum)


def _fmt_row(label, s, w=20):
    if s["n"] == 0:
        return f"{label:<{w}}{0:>7}{'—':>7}{'—':>9}{'—':>9}{'—':>7}{'—':>9}"
    pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    return (f"{label:<{w}}{s['n']:>7}{s['win']:>7.1f}{s['avgwin']:>9.1f}"
            f"{s['expect']:>9.2f}{pf:>7}{s['mdd']:>9.1f}")


# ===========================================================================
# Main
# ===========================================================================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="universe/universe.csv")
    ap.add_argument("--years", type=float, default=10)
    ap.add_argument("--stop", type=float, default=7.0)
    ap.add_argument("--trail", type=float, default=20.0)
    ap.add_argument("--time-exit", type=int, default=45)
    ap.add_argument("--target", type=float, default=0.0)
    ap.add_argument("--cost-bps", type=float, default=0.0,
                    help="round-trip cost per trade in basis points (e.g. 50 = 0.50%%). "
                         "Rough guide: ~30-50 large-cap, ~80-150 small/microcap.")
    ap.add_argument("--by-regime", action="store_true",
                    help="break results down by market regime (uses the trailing-stop exit)")
    args = ap.parse_args()
    cost_pct = args.cost_bps / 100.0

    try:
        import yfinance  # noqa: F401
    except ImportError:
        print("Missing dependency. Run first:  pip install yfinance"); sys.exit(1)

    universe = load_universe(args.universe)
    print(f"Universe: {len(universe)} symbols from {args.universe} | {args.years}y daily (Yahoo Finance)")

    regime_by_date = {}
    if args.by_regime:
        print("Fetching Nifty + India VIX for regime tagging...")
        regime_by_date = build_regime_by_date(args.years)
        if not regime_by_date:
            print("Could not build regime series (Nifty fetch failed). Aborting."); sys.exit(1)
    print()

    variants = [("stop", "Stop only"), ("trail", f"{args.trail:.0f}%trail"),
                ("time", f"{args.time_exit}-day"), ("combo", f"combo{args.trail:.0f}t")]
    if args.by_regime:
        variants = [("trail", f"{args.trail:.0f}%trail")]  # one exit; split by regime instead

    buckets = {(s, v): [] for s in STRATEGIES for v, _ in variants}
    resolved = 0

    for tkr, exch in universe:
        hist = fetch_history(tkr, exch, args.years)
        if not hist:
            print(f"  {tkr}: not on Yahoo ({exch}) — skipped"); continue
        if len(hist) < 260:
            print(f"  {tkr}: thin history ({len(hist)} bars) — skipped"); continue
        resolved += 1
        H = [b["high"] for b in hist]; L = [b["low"] for b in hist]
        C = [b["close"] for b in hist]; V = [b["volume"] for b in hist]
        ctx = build_ctx(H, L, C, V)
        reg_arr = [regime_by_date.get(b["date"], "UNKNOWN") for b in hist] if args.by_regime else None
        for sname, efn in STRATEGIES.items():
            entries = [efn(ctx, i) for i in range(len(C))]
            for v, _ in variants:
                buckets[(sname, v)] += simulate(C, H, L, entries, v, args.stop, args.trail,
                                                args.time_exit, target_pct=args.target,
                                                regime_arr=reg_arr, cost_pct=cost_pct)
        print(f"  {tkr}: done ({len(hist)} bars)")

    # ---------------- reporting ----------------
    hdr = f"{'':<20}{'Trades':>7}{'Win%':>7}{'AvgWin%':>9}{'Expect%':>9}{'PF':>7}{'MaxDD%':>9}"

    if not args.by_regime:
        print("\n" + "=" * 78)
        print(f"RESULTS  |  {args.universe}  |  {resolved} symbols  |  {args.years}y")
        print("=" * 78)
        print(f"{'Strategy / Exit':<20}{'Trades':>7}{'Win%':>7}{'AvgWin%':>9}{'Expect%':>9}{'PF':>7}{'MaxDD%':>9}")
        print("-" * 78)
        for sname in STRATEGIES:
            print(sname)
            for v, vlabel in variants:
                print("  " + _fmt_row(vlabel, stats(buckets[(sname, v)]), w=18))
        print("=" * 78)
        print("Expect% = mean return/trade · PF = gross win/loss · trailing includes a -%d%% hard stop." % args.stop)
        print(f"Round-trip cost applied: {args.cost_bps:.0f} bps ({cost_pct:.2f}%/trade). Survivorship not modeled.")
        return

    # by-regime: for each strategy, split its trailing-stop trades by entry regime
    REGIMES = ["BULL", "EUPHORIA", "SIDEWAYS", "BEAR", "CRASH"]
    print("\n" + "=" * 78)
    print(f"REGIME BASKET VIEW  |  {args.universe}  |  {resolved} symbols  |  {args.years}y")
    print(f"Exit = {args.trail:.0f}% trailing (+ -{args.stop:.0f}% hard stop). Trade tagged by regime AT ENTRY.")
    print("=" * 78)
    for sname in STRATEGIES:
        trades = buckets[(sname, "trail")]
        print(f"\n{sname}")
        print(hdr)
        by = {r: [t for t in trades if t.regime == r] for r in REGIMES}
        for r in REGIMES:
            if by[r]:
                print(_fmt_row("  " + r, stats(by[r])))
        print(_fmt_row("  ALL", stats(trades)))
    print("\n" + "=" * 78)
    print("Read down each strategy: the regimes where it shows PF>1.3 AND positive Expect% are")
    print("the regimes it belongs in. That's your STRATEGY_BASKETS mapping, from evidence.")
    print(f"Regime = Nifty vs 200-EMA + India VIX (breadth omitted). "
          f"Cost {args.cost_bps:.0f} bps/trade applied; survivorship NOT modeled.")


if __name__ == "__main__":
    main()
