# -*- coding: utf-8 -*-
"""cb-weekly 每周自动运行 (GitHub Actions, 北京时间周五 11:00 触发, 通常 13:00 前完成)
流程: 拉实时快照 -> 结算上期持仓收益 -> 更新溢价率中枢 -> 排雷选出新一期双低20只 -> 写回 data/
排雷: 价格≥100 + 评级≥AA- + 正股基本面 + 剩余规模≥0.3亿 (Tushare)
幂等: 同一天重复运行会覆盖当天记录而不是重复追加
注意: 11:00 触发为预留 Actions 调度延迟 (实测延迟可达 3h), 数据为触达时点的行情快照
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
                       turnover, holdings, prev, missing, forced, force_warn):
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
    if forced:
        forced_items = [f"{h['code']} {h['name']} (转股价值¥{h['conv_value']:.0f})" for h in forced]
        card["card"]["elements"].append({"tag": "div", "text": {"tag": "lark_md",
            "content": f"🔥 **强赎预警 {len(forced)} 只** — 转股价值≥130, 可能触发强赎:\n" + "\n".join(f"  · {x}" for x in forced_items)}})
    if force_warn:
        warn_items = []
        for code, info in force_warn.items():
            name = info.get('name', code)
            day = f" 最后交易 {info['last_day']}" if info.get('last_day') and info['last_day'] != 'NaT' else ''
            price = f" 强赎价 ¥{info['redeem_price']:.2f}" if info.get('redeem_price') else ''
            warn_items.append(f"  · {code} {name} — {info['status']}{day}{price}")
        if warn_items:
            card["card"]["elements"].insert(2, {"tag": "div", "text": {"tag": "lark_md",
                "content": f"⚠️ **本期排除 {len(warn_items)} 只强赎券**:\n" + "\n".join(warn_items)}})
    if missing:
        card["card"]["elements"].append({"tag": "div", "text": {"tag": "lark_md",
            "content": f"⚠️ {len(missing)} 只退市/已排除: {', '.join(missing)}"}})
    card["card"]["elements"].append({"tag": "hr"})
    card["card"]["elements"].append({"tag": "note",
        "elements": [{"tag": "plain_text", "content": f"GitHub Actions 自动生成 · {date}"}]})

    req = urllib.request.Request(webhook,
        data=json.dumps(card).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    for attempt in range(3):
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            body = json.loads(resp.read().decode("utf-8"))
            if body.get("code") != 0:
                raise RuntimeError(f"飞书 API 返回错误: code={body.get('code')} msg={body.get('msg', '')}")
            return
        except Exception as e:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))

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
spot["conv_value"] = pd.to_numeric(spot["转股价值"], errors="coerce")
spot["rating"] = spot["信用评级"].astype(str).str.strip()
spot["name"] = spot["债券简称"].astype(str)
live = spot[spot.price.gt(0) & spot.prem.between(-40, 500)].copy()
live["dl"] = live.price + live.prem

# ---------- 排除未上市债券 ----------
if "上市时间" in spot.columns:
    listed = pd.to_datetime(spot["上市时间"], errors="coerce")
    pre_list = (spot["code"].isin(live.code)) & listed.isna()
    n_pre = pre_list.sum()
    if n_pre:
        pre_codes = spot.loc[pre_list, ["code", "name"]].values.tolist()
        print(f"排除 {n_pre} 只未上市转债: {', '.join(f'{c} {n}' for c, n in pre_codes)}")
        live = live[live.code.isin(spot.loc[~pre_list, "code"])]
print(f"存续转债 {len(live)} 只 @ {today}")

# ---------- 加载 Tushare remain_size (排除微型债) ----------
size_ok = set()
size_cache = DATA / "remain_size.csv"
if size_cache.exists():
    sz_df = pd.read_csv(size_cache, dtype=str)
    sz_df['remain_size_yi'] = pd.to_numeric(sz_df['remain_size'], errors='coerce') / 100_000_000
    size_ok = set(sz_df[sz_df['remain_size_yi'] >= MIN_SIZE_YI]['code6'])
    cache_mtime = datetime.fromtimestamp(size_cache.stat().st_mtime).strftime("%Y-%m-%d")
    cache_days = (datetime.now() - datetime.fromtimestamp(size_cache.stat().st_mtime)).days
    print(f"剩余规模≥{MIN_SIZE_YI}亿: {len(size_ok)} 只缓存 (更新于 {cache_mtime})")
    if cache_days > 90:
        print(f"⚠️ remain_size 缓存已 {cache_days} 天未更新, 规模数据可能过时")
else:
    size_ok = set(live.code)
    print("无 remain_size 缓存, 跳过规模过滤")

# ---------- 空数据保护 ----------
if not live.empty:
    center_raw = live.prem.median()
else:
    print("无有效存续转债, 本周跳过"); sys.exit(1)
if pd.isna(center_raw):
    print("溢价率中位数为空, 本周跳过"); sys.exit(1)
center = float(center_raw)

# ---------- 载入历史 (首次运行用默认值) ----------
try:
    hist = pd.read_csv(DATA / "premium_history.csv", parse_dates=["date"]).set_index("date")["median_prem"]
except (FileNotFoundError, KeyError):
    hist = pd.Series(dtype=float)
try:
    mined = set(pd.read_csv(DATA / "mined_stocks.csv")["stock"].astype(str).str.zfill(6))
except FileNotFoundError:
    mined = set()
try:
    records = json.load(open(DATA / "holdings.json", encoding="utf-8"))
except (FileNotFoundError, json.JSONDecodeError):
    records = []

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
            # 退市/强赎: 按面值100近似 (实际=面值+当期利息, 误差通常<2%)
            missing.append(h["code"]); rets.append(100.0 / h["price"] - 1)
        else:
            rets.append(p_now / h["price"] - 1)
    week_return = float(np.mean(rets)) if rets else 0.0
    # NaN/Inf 防护 (价格异常导致)
    if not np.isfinite(week_return):
        print(f"⚠️ 周收益异常 ({week_return}), 重置为 0"); week_return = 0.0

# ---------- 溢价率中枢 ----------
hist.loc[pd.Timestamp(today)] = round(center, 2)
hist = hist[~hist.index.duplicated(keep="last")].sort_index()
window = hist.tail(756)
pct3y = float((window <= center).mean() * 100) if len(window) > 0 else 50.0
light = "red" if pct3y > 85 else ("yellow" if pct3y > 60 else "green")

# ---------- 排雷 + 新一期名单 ----------
# 加载强赎黑名单 (防线①: JSL公告)
force_redeem = set()
force_warn = {}   # code -> {status, last_day, redeem_price}
for attempt in range(3):
    try:
        redeem_df = ak.bond_cb_redeem_jsl()
        for _, r in redeem_df.iterrows():
            code = str(r['代码']).zfill(6)
            st = str(r.get('强赎状态', '')).strip()
            if st in ('已公告强赎', '公告要强赎'):
                force_redeem.add(code)
                force_warn[code] = {
                    'name': str(r.get('名称', '')),
                    'status': st,
                    'last_day': str(r.get('最后交易日', ''))[:10] if pd.notna(r.get('最后交易日')) else '',
                    'redeem_price': float(r['强赎价']) if pd.notna(r.get('强赎价')) else None,
                }
        print(f"强赎黑名单: {len(force_redeem)} 只 (已公告+公告要强赎)")
        break
    except Exception as e:
        if attempt == 2:
            print(f"⚠️ 强赎数据拉取3次失败, 防线①降级: {e}")
        else:
            time.sleep(3 * (attempt + 1))

# 强赎检测第二道防线: 转股价/转股价值为NaN的券 (如精达转债, API可能漏掉)
conv_dead = set()
nan_cp = pd.to_numeric(spot["转股价"], errors="coerce").isna()
nan_cv = pd.to_numeric(spot["转股价值"], errors="coerce").isna()
dead_mask = nan_cp | nan_cv
dead_codes = set(spot.loc[dead_mask & spot["code"].isin(live.code), "code"])
conv_dead = dead_codes - force_redeem
if conv_dead:
    dead_info = spot[spot["code"].isin(conv_dead)][["code", "name"]].values.tolist()
    print(f"转股异常(转股价/转股价值NaN): {len(conv_dead)} 只 — {', '.join(f'{c} {n}' for c, n in dead_info)}")
    for code in conv_dead:
        info = spot[spot["code"] == code]
        force_warn[code] = {
            'name': str(info["name"].values[0]) if len(info) else code,
            'status': '转股异常(转股价/转股价值NaN)',
            'last_day': '',
            'redeem_price': None,
        }

qual = live[(live.price >= 100) & live.rating.isin(GOOD) & ~live.stk.isin(mined) & live.code.isin(size_ok)]
n_before = len(qual)
all_exclude = force_redeem | conv_dead
if all_exclude:
    qual = qual[~qual.code.isin(all_exclude)]
    n_ex = n_before - len(qual)
    if n_ex:
        print(f"排除 {n_ex} 只强赎/异常券")
top = qual.nsmallest(N, "dl")
n_got = len(top)
# ---------- 防线③: bond_zh_cov_info 抽查最终名单 (到期/退市) ----------
info_exclude = set()
if n_got > 0:
    top_codes = top.code.tolist()
    for code in top_codes:
        try:
            info = ak.bond_zh_cov_info(symbol=code)
            is_redeem = str(info['IS_REDEEM'].values[0]) if 'IS_REDEEM' in info.columns else ''
            delist = info['DELIST_DATE'].values[0] if 'DELIST_DATE' in info.columns else None
            if is_redeem == '是' and pd.notna(delist):
                name = str(info['SECURITY_NAME_ABBR'].values[0]) if 'SECURITY_NAME_ABBR' in info.columns else code
                dl_str = pd.Timestamp(delist).strftime('%Y-%m-%d')
                info_exclude.add(code)
                force_warn[code] = {'name': name, 'status': f'到期/退市(DELIST={dl_str})', 'last_day': dl_str, 'redeem_price': None}
                print(f"  抽查排除: {code} {name} DELIST={dl_str}")
            time.sleep(0.3)
        except Exception as e:
            pass  # 单只查询失败不影响整体
    if info_exclude:
        initial_ex = len(info_exclude)
        qual = qual[~qual.code.isin(info_exclude)]
        # 取 N + exclude_count 只, 对新增递补券也做抽查
        top = qual.nsmallest(N + initial_ex, "dl")
        new_codes = [c for c in top.code.tolist() if c not in top_codes]
        for code in new_codes:
            try:
                info = ak.bond_zh_cov_info(symbol=code)
                is_redeem = str(info['IS_REDEEM'].values[0]) if 'IS_REDEEM' in info.columns else ''
                delist = info['DELIST_DATE'].values[0] if 'DELIST_DATE' in info.columns else None
                if is_redeem == '是' and pd.notna(delist):
                    name = str(info['SECURITY_NAME_ABBR'].values[0]) if 'SECURITY_NAME_ABBR' in info.columns else code
                    dl_str = pd.Timestamp(delist).strftime('%Y-%m-%d')
                    info_exclude.add(code)
                    force_warn[code] = {'name': name, 'status': f'到期/退市(DELIST={dl_str})', 'last_day': dl_str, 'redeem_price': None}
                    print(f"  递补复查排除: {code} {name} DELIST={dl_str}")
                time.sleep(0.3)
            except Exception:
                pass
        if len(info_exclude) > initial_ex:
            qual = qual[~qual.code.isin(info_exclude)]
        top = qual.nsmallest(N, "dl")
        n_got = len(top)
        print(f"抽查排除 {len(info_exclude)} 只, 递补后 {n_got} 只")
if n_got < N:
    print(f"⚠️ 合格券仅 {n_got} 只 (不足 {N}), 检查过滤条件: 价格≥100={len(live[live.price>=100])} 评级OK={len(live[live.rating.isin(GOOD)])} 未排雷={len(live[~live.stk.isin(mined)])} 规模OK={len(live[live.code.isin(size_ok)])}")
new_hold = [{"code": r.code, "name": r.name, "price": round(r.price, 2), "prem": round(r.prem, 1),
             "dl": round(r.dl, 1), "rating": r.rating,
             "conv_value": round(r.conv_value, 2) if pd.notna(r.conv_value) else None}
            for r in top.itertuples()]
# 强赎预警: 转股价值≥130 的券
forced = [h for h in new_hold if h["conv_value"] and h["conv_value"] >= 130]
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
                           turnover, new_hold, prev, missing, forced, force_warn)
        print(f"📱 飞书已推送: {today}")
    except Exception as e:
        print(f"⚠️ 飞书推送失败 (不影响选券结果): {e}")
