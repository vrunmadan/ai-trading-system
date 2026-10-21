from sim import *
start = int(np.argmax(cal >= pd.Timestamp("2016-07-01")))
res = {}; regs = {}
for k in PCT:
    regs[k] = regimes(k)
    rr = pd.Series(regs[k][start:])
    dist = (rr.value_counts(normalize=True)*100).round(0).to_dict()
    c, tr = portfolio(regs[k], start)
    s = summarize(c, tr, start)
    # gate quality: what did trades blocked look like? signals firing on CASH days, 60d fwd ret
    res[k] = s
    print(f"{k:20s}", s, "| regime days%:", dist, flush=True)
c, tr = portfolio(regs["NIFTY50"], start, gate=False, use_invalidation=False)
print(f"{'NO_GATE (always on)':20s}", summarize(c, tr, start))
c, tr = portfolio(regs["NIFTY50"], start, cash_yield=0.065)
print(f"{'NIFTY50 + 6.5% cash':20s}", summarize(c, tr, start))
pickle.dump(regs, open("regs.pkl","wb"))
# current reading per index
for k in PCT:
    i = N-1
    print(k, "today:", regs[k][i], "pct_vs_200ema=%.1f" % PCT[k].values[i], "vix=%.1f" % vix.values[i], "breadth=%.0f" % breadth[i])
