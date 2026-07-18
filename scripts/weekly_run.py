# -*- coding: utf-8 -*-
"""cb-weekly 每周自动运行 (GitHub Actions, 北京时间周五 14:35 前后)
流程: 拉实时快照 -> 结算上周持仓收益 -> 更新溢价率中枢 -> 排雷选出新一期双低20只 -> 写回 data/
幂等: 同一天重复运行会覆盖当天记录而不是重复追加
"""
import socket
socket.setdefaulttimeout(25)
import akshare as ak
import pandas as pd
import numpy as np
import json, os, sys, time
from pathlib import Path
from datetime import datetime, timezone, timedelta

DATA = Path(os.environ.get("CB_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
N = 20
COST_RT = 0.001            # 双边成本, 计入净值
GOOD = {"AAA", "AA+", "AA", "AA-", "AA+sti", "AAsti", "AA-sti"}
CST = timezone(timedelta(hours=8))
today = datetime.now(CST).strftime("%Y-%m-%d")

# ---------- 拉取实时快照 (带重试) ----------
spot = None
for attempt in range(4):
    try:
        spot = ak.bond_zh_cov()
        if spot is not None and len(spot) > 100:
            break
    except Exception as e:
        print(f"快照第{attempt+1}次失败: {str(e)[:80]}", flush=True)
        time.sleep(15 * (attempt + 1))
if spot is None or len(spot) < 100:
    print("快照拉取失败, 本周跳过 (数据未改动)"); sys.exit(1)

spot["code"] = spot["债券代码"].astype(str).str.zfill(6)
spot["stk"] = spot["正股代码"].astype(str).str.zfill(6)
spot["price"] = pd.to_numeric(spot["债现价"], errors="coerce")
spot["prem"] = pd.to_numeric(spot["转股溢价率"], errors="coerce")
spot["rating"] = spot["信用评级"].astype(str).str.strip()
spot["name"] = spot["债券简称"].astype(str)
live = spot[spot.price.gt(0) & spot.prem.between(-40, 500)].copy()
live["dl"] = live.price + live.prem
print(f"存续转债 {len(live)} 只 @ {today}")

# ---------- 载入历史 ----------
hist = pd.read_csv(DATA / "premium_history.csv", parse_dates=["date"]).set_index("date")["median_prem"]
mined = set(pd.read_csv(DATA / "mined_stocks.csv")["stock"].astype(str).str.zfill(6))
records = json.load(open(DATA / "holdings.json", encoding="utf-8"))

# ---------- 结算上周持仓 ----------
prev = records[-1] if records else None
if prev and prev["date"] == today:                 # 同日重跑: 回退到上一条
    records = records[:-1]
    prev = records[-1] if records else None
week_return, turnover, missing = None, None, []
if prev:
    price_map = dict(zip(live.code, live.price))
    rets = []
    for h in prev["holdings"]:
        p_now = price_map.get(h["code"])
        if p_now is None:
            missing.append(h["code"]); rets.append(0.0)   # 退市/强赎: 按0近似, 记录明细
        else:
            rets.append(p_now / h["price"] - 1)
    week_return = float(np.mean(rets)) if rets else 0.0

# ---------- 溢价率中枢 ----------
center = float(live.prem.median())
hist.loc[pd.Timestamp(today)] = round(center, 2)
hist = hist[~hist.index.duplicated(keep="last")].sort_index()
window = hist.tail(756)
pct3y = float((window <= center).mean() * 100)
light = "red" if pct3y > 85 else ("yellow" if pct3y > 60 else "green")

# ---------- 排雷 + 新一期名单 ----------
qual = live[(live.price >= 100) & live.rating.isin(GOOD) & ~live.stk.isin(mined)]
top = qual.nsmallest(N, "dl")
new_hold = [{"code": r.code, "name": r.name, "price": round(r.price, 2), "prem": round(r.prem, 1),
             "dl": round(r.dl, 1), "rating": r.rating} for r in top.itertuples()]
if prev:
    prev_codes = {h["code"] for h in prev["holdings"]}
    turnover = len([h for h in new_hold if h["code"] not in prev_codes]) / max(len(new_hold), 1)
prev_nav = prev["cum_nav"] if prev else 1.0
cum_nav = prev_nav * (1 + (week_return or 0) - (turnover or 0) * COST_RT) if prev else 1.0

records.append({
    "date": today, "center": round(center, 2), "center_pct3y": round(pct3y, 1), "light": light,
    "week_return": round(week_return, 5) if week_return is not None else None,
    "turnover": round(turnover, 3) if turnover is not None else None,
    "cum_nav": round(cum_nav, 5), "missing": missing, "holdings": new_hold,
})

# ---------- 写回 ----------
hist.rename("median_prem").to_csv(DATA / "premium_history.csv")
json.dump(records, open(DATA / "holdings.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
wr = f"{week_return*100:+.2f}%" if week_return is not None else "—(首期)"
print(f"完成: 中枢{center:.1f}%(3年分位{pct3y:.0f}%,{light}) | 上周收益 {wr} | 净值 {cum_nav:.4f} | 新名单{len(new_hold)}只 换手{(turnover or 0)*100:.0f}%")
if missing:
    print(f"注意: {len(missing)} 只持仓已退市/强赎按0%近似: {missing}")
