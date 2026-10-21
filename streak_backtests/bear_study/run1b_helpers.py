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

def st(rs):
    rs = np.array(rs)*100
    if len(rs)==0: return "n=0"
    w = rs[rs>0]; l = rs[rs<=0]
    return f"n={len(rs):4d} win={len(w)/len(rs)*100:4.0f}% exp={rs.mean():6.1f}% PF={w.sum()/abs(l.sum()):4.2f}"
