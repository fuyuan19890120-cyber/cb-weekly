# -*- coding: utf-8 -*-
"""一次性数据引导: 从本地 ai-capital-ashare/data/cb 导出到 cb-weekly/data
产出: premium_history.csv(全市场溢价率中位数日序列) / mined_stocks.csv(基本面雷股) / holdings.json(首周持仓)"""
import pandas as pd, numpy as np, json
from pathlib import Path

SRC = Path.home() / "ai-capital-ashare" / "data" / "cb"
DST = Path.home() / "cb-weekly" / "data"
DST.mkdir(exist_ok=True)
N = 20
GOOD = {"AAA", "AA+", "AA", "AA-", "AA+sti", "AAsti", "AA-sti"}

# 1) 溢价率中位数历史
frames = []
for f in SRC.glob("[0-9]*.csv"):
    df = pd.read_csv(f, usecols=[0, 5], names=["date", "prem"], header=0)
    frames.append(df)
hp = pd.concat(frames)
hp["date"] = pd.to_datetime(hp["date"], errors="coerce")
hp = hp.dropna()
hp = hp[hp.prem.between(-40, 500)]
med = hp.groupby("date").prem.median().round(2)
med = med[med.index >= "2019-01-01"]
med.rename("median_prem").to_csv(DST / "premium_history.csv")
print(f"premium_history: {len(med)} 天 ({med.index.min().date()} ~ {med.index.max().date()}), 最新 {med.iloc[-1]}%")

# 2) 雷股名单 (与回测同规则: 连续两年扣非亏损 或 负债率>90%, 用最近已披露年报)
mined = []
for f in (SRC / "stock_fund").glob("*.csv"):
    try:
        df = pd.read_csv(f)
        df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
        ann = df[df["日期"].dt.month == 12].sort_values("日期")
        if not len(ann): continue
        prof_col = next((c for c in df.columns if "净利润" in c), None)
        debt_col = next((c for c in df.columns if "资产负债率" in c), None)
        prof = pd.to_numeric(ann[prof_col], errors="coerce")
        debt = pd.to_numeric(ann[debt_col], errors="coerce")
        if (len(prof) >= 2 and prof.iloc[-1] < 0 and prof.iloc[-2] < 0) or (len(debt) and debt.iloc[-1] > 90):
            mined.append(f.stem)
    except Exception:
        continue
pd.Series(sorted(mined), name="stock").to_csv(DST / "mined_stocks.csv", index=False)
print(f"mined_stocks: {len(mined)} 只雷股")

# 3) 首周持仓 (用缓存最后一天 = 2026-07-17 周五收盘, 与实盘起点一致)
lst = pd.read_csv(SRC / "_list.csv", dtype=str)
lst["bond"] = lst["债券代码"].str.zfill(6); lst["stk"] = lst["正股代码"].str.zfill(6)
b2s = dict(zip(lst.bond, lst.stk))
name_map = dict(zip(lst.bond, lst["债券简称"]))
rat = pd.read_csv(SRC / "_ratings.csv", dtype={"债券代码": str})
rat["code"] = rat["债券代码"].str.zfill(6)
rat_map = dict(zip(rat.code, rat["信用评级"].astype(str).str.strip()))
rows = []
for f in SRC.glob("[0-9]*.csv"):
    df = pd.read_csv(f)
    df.columns = ["date","close","bv","cv","bp","prem"][:len(df.columns)]
    last = df.iloc[-1]
    rows.append((f.stem, str(last.date), float(last.close), float(last.prem)))
snap = pd.DataFrame(rows, columns=["code", "date", "close", "prem"])
last_date = snap.date.max()
snap = snap[snap.date == last_date]
snap["rating"] = snap.code.map(rat_map)
snap["stk"] = snap.code.map(b2s)
mined_set = set(mined)
qual = snap[(snap.close >= 100) & snap.rating.isin(GOOD) & ~snap.stk.isin(mined_set) & snap.prem.between(-40, 500)].copy()
qual["dl"] = qual.close + qual.prem
top = qual.nsmallest(N, "dl")
rec = {
    "date": last_date,
    "center": round(float(med.iloc[-1]), 2),
    "center_pct3y": round(float((med.tail(756) <= med.iloc[-1]).mean() * 100), 1),
    "week_return": None, "cum_nav": 1.0, "turnover": None,
    "holdings": [{"code": r.code, "name": name_map.get(r.code, r.code), "price": round(r.close, 2),
                  "prem": round(r.prem, 1), "dl": round(r.dl, 1), "rating": r.rating} for r in top.itertuples()],
}
json.dump([rec], open(DST / "holdings.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"holdings.json 首周({last_date}) {len(top)} 只, 双低均值 {top.dl.mean():.1f}, 中枢分位 {rec['center_pct3y']}%")
