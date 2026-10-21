from run1b_helpers import *
reg = regs["NIFTY50"]
print("Signal-level by ENTRY regime (Nifty50 tagging), 7% stop / 40% trail, 100bps:")
for s in ["52wk_breakout","bb_squeeze_break","adx_bull_strength","golden_cross_ema"]:
    tr = sig_trades(s)
    for r in ["bull","euphoria","sideways","bear","extreme_fear_crash"]:
        print(f"  {s:18s} {r:19s}", st([x for i,x in tr if reg[i]==r]))
