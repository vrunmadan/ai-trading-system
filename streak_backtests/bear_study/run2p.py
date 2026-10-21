import run2 as R
from sim import portfolio, summarize
import numpy as np
for k in ["NIFTY50","BLEND_MID_SMALL"]:
    reg = R.regs[k]; R.isbear[:] = [r == "bear" for r in reg]
    sig = R.signals("rs", .20)
    for slots in (5, 10):
        a = summarize(*portfolio(reg, R.start, slots=slots), R.start)
        c, tr = portfolio(reg, R.start, bear_sig=sig, slots=slots); b = summarize(c, tr, R.start)
        nb = sum(1 for t in tr if t["tag"] == "bear_rs")
        print(f"{k:16s} {slots}slots  without: CAGR {a['CAGR']}% DD {a['MaxDD']}%  | with RS-bear: CAGR {b['CAGR']}% DD {b['MaxDD']}% ({nb} bear trades)")
