import csv, pickle, time, yfinance as yf, pandas as pd, sys
U="/mnt/user-data/uploads/AI Trading System/universe/universe.csv"
tk=[r["Ticker"].strip() for r in csv.DictReader(open(U,encoding="utf-8")) if r["Ticker"].strip()]
data={}
start="2015-06-01"
for k in range(0,len(tk),40):
    batch=[t+".NS" for t in tk[k:k+40]]
    for attempt in range(3):
        try:
            df=yf.download(batch,start=start,auto_adjust=True,progress=False,group_by="ticker",threads=True); break
        except Exception as e: print("err",e); time.sleep(10)
    for t in batch:
        try:
            d=df[t].dropna(subset=["Close"])
            if len(d)>0: data[t[:-3]]=d[["High","Low","Close","Volume"]]
        except KeyError: pass
    print(k,len(data),flush=True); time.sleep(2)
idx={}
for s in ["^NSEI","^NSEMDCP50","NIFTYSMLCAP250.NS","NIFTYMIDCAP150.NS","^INDIAVIX"]:
    idx[s]=yf.download(s,start="2014-01-01",auto_adjust=True,progress=False)["Close"].iloc[:,0].dropna()
pickle.dump((data,idx),open("data.pkl","wb"))
print("tickers",len(tk),"got",len(data))
