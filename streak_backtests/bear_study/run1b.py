from sim import *
regs = pickle.load(open("regs.pkl","rb"))
start = int(np.argmax(cal >= pd.Timestamp("2016-07-01")))
# (a) trade-level: every signal, independent, 7% stop / 40% trail on closes, 100bps
def sig_trades(sname, stop=7, trail=40, cost=1.0):
    out = []
    for t in T:
        d = P[t]; C = d["C"]; s = d[sname]; i = start
        while i < N:
            if s[i]:
                e = C[i]; pk = e; j = i + 1
                while j < N:
                    c = C[j]
                    if not np.isnan(c):
                        pk = max(pk, c)
                        if c <= max(e*(1-stop/100), pk*(1-trail/100)): break
                    j += 1
                j = min(j, N-1)
                out.append((i, C[j]/e - 1 - cost/100)); i = j + 1
            else: i += 1
    return out
ALL = {s: sig_trades(s) for s in ["52wk_breakout","bb_squeeze_break","adx_bull_strength","golden_cross_ema"]}
def st(rs):
    rs = np.array(rs)*100
    if len(rs)==0: return "n=0"
    w = rs[rs>0]; l = rs[rs<=0]
    return f"n={len(rs):4d} win={len(w)/len(rs)*100:4.0f}% exp={rs.mean():6.1f}% PF={w.sum()/abs(l.sum()):4.2f}"
print("Signal-level (union of live basket strategies, each strategy only where the live basket uses it):")
for k, reg in regs.items():
    act, cash = [], []
    for s, tr in ALL.items():
        for i, r in tr:
            rg = reg[i]
            if rg is None: continue
            (act if s in BASKET[rg] else cash).append(r)
    print(f"  {k:20s} ACTIVE {st(act)} | BLOCKED {st(cash)}")
# (b) portfolio robustness grid
print("\nPortfolio grid CAGR/MaxDD (slots x trail):")
for k, reg in regs.items():
    row = []
    for slots in (5, 10):
        for trail in (20, 40):
            c, tr = portfolio(reg, start, slots=slots, trail=trail)
            s = summarize(c, tr, start); row.append(f"{slots}s/{trail}t {s['CAGR']:5.1f}/{s['MaxDD']:6.1f}")
    print(f"  {k:20s}", " | ".join(row))
