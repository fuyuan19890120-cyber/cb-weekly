# -*- coding: utf-8 -*-
"""cb-weekly 双周自动运行 (GitHub Actions, 北京时间周五 14:35 前后, 每两周)
流程: 拉实时快照 -> 结算上期持仓收益 -> 更新溢价率中枢 -> 排雷选出新一期双低20只 -> 写回 data/
排雷: 价格≥100 + 评级≥AA- + 正股基本面 + 剩余规模≥0.3亿 (Tushare)
幂等: 同一天重复运行会覆盖当天记录而不是重复追加
"""
import socket
socket.setdefaulttimeout(25)
import akshare as ak
import pandas as pd
import numpy as np
import json, os, sys, time, sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta

DATA = Path(os.environ.get("CB_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
N = 20
COST_RT = 0.001
MIN_SIZE_YI = 0.3
GOOD = {"AAA", "AA+", "AA", "AA-", "AA+sti", "AAsti", "AA-sti"}
CST = timezone(timedelta(hours=8))
today = datetime.now(CST).strftime("%Y-%m-%d")

LIGHT_EMOJI = {"red": "🔴", "yellow": "🟡", "green": "🟢"}


def _send_feishu_card(webhook, date, center, pct3y, light, week_return, cum_nav,
                       turnover, holdings, prev, missing):
    """发送飞书卡片消息 — 20只双低名单 + 信号灯 + 收益"""
    import urllib.request

    # 持仓表格
    rows = []
    for i, h in enumerate(holdings, 1):
        rows.append(f"{i:2d}. {h['code']} {h['name']}  ¥{h['price']:.1f}  溢价{h['prem']:.1f}%  双低{h['dl']:.1f}  {h['rating']}")

    # 上期变动
    prev_set = {h["code"] for h in prev["holdings"]} if prev else set()
    cur_set = {h["code"] for h in holdings}
    new_in = cur_set - prev_set
    kicked = prev_set - cur_set
    change_line = ""
    if new_in:
        new_items = [f"{h['code']} {h['name']}" for h in holdings if h["code"] in new_in]
        change_line += f"\n🆕 买入 {len(new_in)} 只: {', '.join(new_items)}"
    if kicked:
        kicked_items = [f"{h['code']} {h['name']}" for h in prev["holdings"] if h["code"] in kicked]
        change_line += f"\n🚫 卖出 {len(kicked)} 只: {', '.join(kicked_items)}"

    wr_text = f"{week_return*100:+.2f}%" if week_return is not None else "首期"
    light_text = f"{LIGHT_EMOJI.get(light, '')} {light.upper()}"

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"content": f"📊 双低可转债 · {date}", "tag": "plain_text"},
                "template": "blue"
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md",
                 "content": f"**信号灯 {light_text}**　　溢价率中枢 **{center:.1f}%** (3年分位 {pct3y:.0f}%)"}},
                {"tag": "div", "text": {"tag": "lark_md",
                 "content": f"上期收益 {wr_text}　|　净值 **{cum_nav:.4f}**　|　换手 {(turnover or 0)*100:.0f}%"}},
                {"tag": "hr"},
                {"tag": "div", "text": {"tag": "lark_md",
                 "content": "**本期持仓 20 只** (等权, 双低排序):\n\n" + "\n".join(rows)}},
            ]
        }
    }
    if change_line.strip():
        card["card"]["elements"].insert(3, {"tag": "div", "text": {"tag": "lark_md",
            "content": change_line.strip()}})
    if missing:
        card["card"]["elements"].append({"tag": "div", "text": {"tag": "lark_md",
            "content": f"⚠️ {len(missing)} 只退市/强赎: {', '.join(missing)}"}})
    card["card"]["elements"].append({"tag": "hr"})
    card["card"]["elements"].append({"tag": "note",
        "elements": [{"tag": "plain_text", "content": f"GitHub Actions 自动生成 · {date}"}]})

    req = urllib.request.Request(webhook,
        data=json.dumps(card).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10)

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

# ---------- 加载 Tushare remain_size (排除微型债) ----------
size_ok = set()
size_cache = DATA / "remain_size.csv"
if size_cache.exists():
    sz_df = pd.read_csv(size_cache, dtype=str)
    sz_df['remain_size_yi'] = pd.to_numeric(sz_df['remain_size'], errors='coerce') / 100_000_000
    size_ok = set(sz_df[sz_df['remain_size_yi'] >= MIN_SIZE_YI]['code6'])
    print(f"剩余规模≥{MIN_SIZE_YI}亿: {len(size_ok)} 只缓存")
else:
    # GitHub Actions 无法连 Tushare, 使用宽松默认(全部通过)
    size_ok = set(live.code)
    print("无 remain_size 缓存, 跳过规模过滤")

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
qual = live[(live.price >= 100) & live.rating.isin(GOOD) & ~live.stk.isin(mined) & live.code.isin(size_ok)]
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

# ---------- Redis IPC: 可选发布 ----------
if os.environ.get("CB_REDIS_URL") or os.environ.get("CB_REDIS_HOST"):
    try:
        from scripts.redis_config import build_signal, push_signal
        meta = {"date": today, "center": round(center, 2), "center_pct3y": round(pct3y, 1), "light": light}
        sig = build_signal(new_hold, meta)
        push_signal(sig)
        print(f"📡 Redis 已发布: {today}")
    except Exception as e:
        print(f"⚠️ Redis 发布失败 (不影响选券结果): {e}")

# ---------- 飞书通知: 可选推送 ----------
FEISHU_WEBHOOK = os.environ.get("CB_FEISHU_WEBHOOK", "")
if FEISHU_WEBHOOK:
    try:
        _send_feishu_card(FEISHU_WEBHOOK, today, center, pct3y, light, week_return, cum_nav,
                           turnover, new_hold, prev, missing)
        print(f"📱 飞书已推送: {today}")
    except Exception as e:
        print(f"⚠️ 飞书推送失败 (不影响选券结果): {e}")
