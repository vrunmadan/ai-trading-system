import pickle, numpy as np, pandas as pd, sys, json
import importlib.util
# --- live classifier scoring, copied verbatim via import (no Kite needed for these two fns)
src = open("/mnt/user-data/uploads/AI Trading System/researcher/regime_classifier.py").read()
ns = {}
import re
start = src.index("# Regime thresholds") if "# Regime thresholds" in src else 0
# execute only constants + the two pure functions
consts = "\n".join(l for l in src.splitlines() if re.match(r"^(VIX_|NIFTY_|BREADTH_)\w+\s*=", l))
f1 = src[src.index("def _score_components"):src.index("def _map_score_to_regime")]
f2 = src[src.index("def _map_score_to_regime"):src.index("# ---------------------------------------------------------------------------\n# Main public")]
enum = src[src.index("class Regime"):src.index("class Regime")+400].split("\n\n")[0]
exec("import os\nfrom typing import *\nfrom enum import Enum\n" + consts + "\n" + enum + "\n" + f1 + "\n" + f2, ns)
score_fn, map_fn, Regime = ns["_score_components"], ns["_map_score_to_regime"], ns["Regime"]

cal, P = pickle.load(open("prep.pkl", "rb"))
_, idx = pickle.load(open("data.pkl", "rb"))
T = list(P); N = len(cal)
def ema(a, p):
    k = 2/(p+1); out = np.empty(len(a)); e = a[0]
    for i, x in enumerate(a): e = x*k + e*(1-k); out[i] = e
    return out
def pct_vs_200(sym):
    s = idx[sym]; s = s[~s.index.duplicated()]
    e = pd.Series(ema(s.values, 200), index=s.index)
    p = ((s - e) / e * 100)
    first = s.index[0]
    p[s.index < first + pd.Timedelta(days=300)] = np.nan   # need ~200 bars of EMA warmup
    return p.reindex(cal).ffill()
vix = idx["^INDIAVIX"]; vix = vix[~vix.index.duplicated()].reindex(cal).ffill()
vix5 = (vix / vix.shift(5) - 1) * 100
A50 = np.array([P[t]["above50"] for t in T]); H50 = np.array([P[t]["has50"] for t in T])
breadth = A50.sum(0) / np.maximum(H50.sum(0), 1) * 100

PCT = {"NIFTY50": pct_vs_200("^NSEI"), "MIDCAP50": pct_vs_200("^NSEMDCP50"),
       "SMALLCAP250": pct_vs_200("NIFTYSMLCAP250.NS")}
PCT["BLEND_MID_SMALL"] = (PCT["MIDCAP50"] + PCT["SMALLCAP250"]) / 2
PCT["SMALLCAP250_SCALED"] = PCT["SMALLCAP250"] / 1.5   # same bands, scaled for ~1.5x higher index vol

def regimes(key):
    out = []
    p = PCT[key].values
    for i in range(N):
        if np.isnan(p[i]) or np.isnan(vix.values[i]): out.append(None); continue
        sc, _ = score_fn(p[i], vix.values[i], np.nan_to_num(vix5.values[i]), breadth[i])
        r, _ = map_fn(sc, p[i], vix.values[i]); out.append(r.value)
    return out

BASKET = {"bull": ["52wk_breakout","bb_squeeze_break","adx_bull_strength"],
          "euphoria": ["adx_bull_strength","bb_squeeze_break","52wk_breakout"],
          "sideways": ["52wk_breakout","bb_squeeze_break","golden_cross_ema"],
          "bear": [], "extreme_fear_crash": []}
INVALID = {"bull": ["bear","extreme_fear_crash"], "euphoria": ["bear","extreme_fear_crash"],
           "sideways": ["extreme_fear_crash"], "bear": [], "extreme_fear_crash": []}

def portfolio(reg, start, bear_sig=None, slots=5, stop=7.0, trail=40.0, cost=1.0, cash_yield=0.0,
              use_invalidation=True, gate=True):
    """Daily EOD sim matching live: entries at close, exits at close when close <= max(hard, trail from peak close),
    paper-mode regime invalidation closes. 20%/slot of current equity. cost=round-trip % per trade."""
    eq_cash = 1_000_000.0; pos = {}; trades = []; curve = []
    dly = (1 + cash_yield) ** (1/250) - 1
    for i in range(start, N):
        r = reg[i]
        # exits
        for t in list(pos):
            p = pos[t]; c = P[t]["C"][i]
            if np.isnan(c): continue
            p["peak"] = max(p["peak"], c)
            stp = max(p["entry"] * (1 - stop/100), p["peak"] * (1 - trail/100))
            inv = use_invalidation and r in INVALID.get(p["reg"], [])
            if c <= stp or inv:
                ret = c / p["entry"] - 1 - cost/100
                eq_cash += p["val"] * (1 + ret)
                trades.append(dict(t=t, reg=p["reg"], ret=ret*100, bars=i-p["i"], why="inv" if inv and c > stp else "stop",
                                   ent=str(cal[p["i"]].date()), ex=str(cal[i].date()), tag=p["tag"]))
                del pos[t]
        # mark
        mv = sum(p["val"] * P[t]["C"][i] / p["entry"] if not np.isnan(P[t]["C"][i]) else p["val"] for t, p in pos.items())
        eq = eq_cash + mv
        # entries
        strats = BASKET.get(r, []) if gate else ["52wk_breakout","bb_squeeze_break","adx_bull_strength","golden_cross_ema"]
        cands = []
        if r is not None:
            for t in T:
                if t in pos: continue
                d = P[t]
                if strats and any(d[s][i] for s in strats): cands.append((d["volr"][i], t, "base"))
                elif bear_sig is not None and r == "bear" and bear_sig[t][i]: cands.append((d["volr"][i], t, "bear_rs"))
        cands.sort(reverse=True)
        for _, t, tag in cands:
            if len(pos) >= slots: break
            size = eq / slots
            if size > eq_cash + 1: break
            eq_cash -= size
            pos[t] = dict(entry=P[t]["C"][i], peak=P[t]["C"][i], val=size, reg=r, i=i, tag=tag)
        eq_cash *= (1 + dly) if eq_cash > 0 else 1
        curve.append(eq)
    for t, p in pos.items():   # mark open at last close
        c = P[t]["C"][N-1]; trades.append(dict(t=t, reg=p["reg"], ret=(c/p["entry"]-1-cost/100)*100, bars=N-1-p["i"], why="open",
                                               ent=str(cal[p["i"]].date()), ex="open", tag=p["tag"]))
    return np.array(curve), trades

def summarize(curve, trades, start):
    yrs = (cal[-1] - cal[start]).days / 365.25
    cagr = (curve[-1] / curve[0]) ** (1/yrs) - 1
    peak = np.maximum.accumulate(curve); mdd = ((curve - peak) / peak).min()
    rets = np.array([t["ret"] for t in trades]) if trades else np.array([0.0])
    w = rets[rets > 0]; l = rets[rets <= 0]
    pf = w.sum() / abs(l.sum()) if l.sum() != 0 else np.inf
    return dict(CAGR=round(cagr*100,1), MaxDD=round(mdd*100,1), MAR=round(cagr/abs(mdd),2) if mdd else None,
                trades=len(trades), win=round(len(w)/max(len(rets),1)*100,1), exp=round(rets.mean(),2), PF=round(pf,2),
                final=round(curve[-1]/1e5,1))
