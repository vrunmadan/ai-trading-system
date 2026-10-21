from run1b_helpers import *
import collections
reg = regs["NIFTY50"]
nif = idx["^NSEI"]; nif = nif[~nif.index.duplicated()].reindex(cal).ffill().values
def ret63(C): r = np.full(N, np.nan); r[63:] = C[63:]/C[:-63]-1; return r
NR = ret63(nif)
isbear = np.array([r == "bear" for r in reg]); hostile = np.array([r in ("bear","extreme_fear_crash") for r in reg])
bear_days = np.where(isbear & (np.arange(N) >= start))[0]
def signals(kind, k=0.2):
    out = {}
    for t in T:
        d = P[t]; C = d["C"]
        if kind == "any":
            s = np.zeros(N, bool); s[bear_days[::10]] = True; s &= ~np.isnan(C)
        else:
            s = d["52wk_breakout"] & isbear
            if kind == "rs":
                rs = ret63(C) - NR
                s = s & (C > d["e200"]) & (d["e50"] > d["e200"]) & (np.nan_to_num(rs, nan=-9) >= k)
        out[t] = s
    return out
def run(sig, mode, stop=7, trail=40, cost=1.0):
    res = []
    for t in T:
        C = P[t]["C"]; s = sig[t]; i = start
        while i < N:
            if s[i] and not np.isnan(C[i]):
                e = C[i]; pk = e; j = i + 1
                while j < N:
                    c = C[j]
                    if not np.isnan(c):
                        pk = max(pk, c)
                        tl = trail if mode == "live" else 20
                        if c <= max(e*(1-stop/100), pk*(1-tl/100)): break
                        if mode == "inbear" and not hostile[j]: break
                    j += 1
                j = min(j, N-1)
                res.append((i, C[j]/e - 1 - cost/100, j-i)); i = j + 1
            else: i += 1
    return res
def st2(rs):
    if not rs: return "n=0"
    r = np.array([x[1] for x in rs])*100; w = r[r>0]; l = r[r<=0]
    pf = w.sum()/abs(l.sum()) if l.sum() else np.inf
    yrs = collections.Counter(str(cal[x[0]].year) for x in rs)
    return f"n={len(r):4d} win={len(w)/len(r)*100:3.0f}% exp={r.mean():6.1f}% med={np.median(r):6.1f}% PF={pf:5.2f} hold={np.median([x[2] for x in rs]):4.0f}d years={dict(sorted(yrs.items()))}"
print("bear days in test window:", len(bear_days), "episodes by year:", dict(sorted(collections.Counter(str(cal[i].year) for i in bear_days).items())))
S = {"CONTROL any stock": signals("any"), "CONTROL plain 52wk": signals("52wk"),
     "RS>=10pp": signals("rs", .10), "RS>=20pp": signals("rs", .20), "RS>=30pp": signals("rs", .30)}
for mode, label in [("live", "LIVE EXIT 7% stop / 40% trail"), ("inbear", "IN-BEAR ONLY: 7% stop / 20% trail / exit when regime turns non-hostile")]:
    print("\n" + label)
    for k, s in S.items(): print(f"  {k:20s}", st2(run(s, mode)))
