# Precompute per-ticker indicator arrays + entry signals aligned to a common calendar.
import pickle, numpy as np, pandas as pd
from multiprocessing import Pool
import bt_orig as B

data, idx = pickle.load(open("data.pkl","rb"))
cal = idx["^NSEI"].index
cal = cal[cal >= "2015-06-01"]

def one(item):
    t, d = item
    d = d[~d.index.duplicated()].reindex(cal)   # align; NaN where not trading/listed
    valid = d["Close"].notna().values
    dd = d.ffill()
    first = np.argmax(valid) if valid.any() else len(cal)
    sub = dd.iloc[first:]
    if len(sub) < 260: return t, None
    H, L, C, V = [sub[c].astype(float).tolist() for c in ["High","Low","Close","Volume"]]
    n = len(C)
    rsi = B.rsi_series(C); st = B.supertrend_series(H, L, C)
    sma50 = B.sma_series(C, 50); ema50 = B.ema_series(C, 50); ema200 = B.ema_series(C, 200)
    pdi, mdi, adx = B.adx_series(H, L, C)
    up, lo, pos, bw = B.bb_series(C)
    Cs = pd.Series(C); Vs = pd.Series(V); bws = pd.Series(bw)
    hi52 = Cs.rolling(252, min_periods=1).max().values
    volr = (Vs / Vs.shift(1).rolling(20, min_periods=1).mean()).fillna(0).replace(np.inf, 0).values
    # 20th pct of bw over [i-100, i) (backtest: sorted(window)[int(.2*len)])
    bwq = bws.shift(1).rolling(100).quantile(0.20, interpolation="lower").values
    i = np.arange(n); C_ = np.array(C); st_ = np.array(st); rsi_ = np.array(rsi)
    e200 = np.array(ema200); e50 = np.array(ema50); s50 = np.array(sma50)
    adx_ = np.array(adx); pdi_ = np.array(pdi); mdi_ = np.array(mdi)
    up_ = np.array(up); bw_ = np.array(bw)
    sig = {}
    sig["52wk_breakout"] = (i >= 60) & (C_ >= .99*hi52) & (volr >= 1.5) & (rsi_ >= 50) & (rsi_ <= 70) & (st_ == "GREEN")
    prev = lambda a: np.r_[a[:1], a[:-1]]
    sig["golden_cross_ema"] = (i >= 205) & (e50 > e200) & (prev(e50) <= prev(e200))
    sig["adx_bull_strength"] = (i >= 40) & (adx_ > 25) & (prev(adx_) <= 25) & (pdi_ > mdi_) & (C_ > s50)
    sq = (prev(bw_) <= np.nan_to_num(bwq, nan=-1)) & (prev(bw_) > 0)
    sig["bb_squeeze_break"] = (i >= 120) & sq & (C_ > up_) & (prev(C_) <= prev(up_)) & (rsi_ > 50)
    pad = lambda a, fill: np.r_[np.full(first, fill), np.asarray(a, dtype=float if fill is np.nan else a.dtype if hasattr(a,'dtype') else float)]
    out = dict(first=first, valid=valid,
               C=np.r_[np.full(first, np.nan), C_],
               above50=np.r_[np.zeros(first, bool), (C_ > s50) & (i >= 49)],
               has50=np.r_[np.zeros(first, bool), i >= 49],
               e200=np.r_[np.full(first, np.nan), e200], e50=np.r_[np.full(first, np.nan), e50],
               volr=np.r_[np.zeros(first), volr])
    for k, v in sig.items(): out[k] = np.r_[np.zeros(first, bool), v] & valid
    return t, out

if __name__ == "__main__":
    with Pool(8) as p: res = dict(p.map(one, list(data.items())))
    res = {k: v for k, v in res.items() if v is not None}
    pickle.dump((cal, res), open("prep.pkl", "wb"))
    print("prepared", len(res), "of", len(data), "calendar", cal[0].date(), cal[-1].date(), len(cal))
