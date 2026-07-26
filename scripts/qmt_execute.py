# -*- coding: utf-8 -*-
"""Windows QMT 端: 读取订单 CSV, 对比当前持仓, 生成调仓指令并执行
   用法: python qmt_execute.py [--dry-run] [--confirm]
        --dry-run  只对比不执行 (默认)
        --confirm  确认后自动执行

   前置: MiniQMT 已登录, xtquant 已安装
"""
import csv, time, sys
from pathlib import Path
from datetime import datetime

# ============================================================
# 配置 (按你的实际路径修改)
# ============================================================
ORDERS_CSV = Path(__file__).resolve().parent.parent / "data" / "orders.csv"
QMT_PATH = r"C:\Program Files\国金证券QMT"          # MiniQMT 安装路径, 按实际修改
ACCOUNT = ""                                         # 资金账号, 留空自动获取
MIN_CASH = 5_000                                     # 保留现金, 低于此值不买
PRICE_TICK = 0.01                                    # 挂单价: 买一价 + tick
MAX_WEIGHT = 0.08                                    # 单只上限 8% (等权=5%, 留缓冲)

# ============================================================
# 初始化 QMT 连接
# ============================================================
def init_qmt():
    import xtquant.xttrader as xttrader
    from xtquant import xtdata

    # 下载转债实时行情 (缓存)
    xtdata.download_history_data2([], period='1d')

    session = int(time.time())
    trader = xttrader.XtQuantTrader(QMT_PATH, session)

    # 连接
    trader.start()
    connect_result = trader.connect()
    if connect_result != 0:
        raise RuntimeError(f"QMT 连接失败, 返回码: {connect_result}")
    print("QMT 连接成功")

    # 订阅账户
    if ACCOUNT:
        trader.subscribe(ACCOUNT)
    else:
        accounts = trader.query_accounts()
        if not accounts:
            raise RuntimeError("未找到账户, 请检查 MiniQMT 是否已登录")
        trader.subscribe(accounts[0])
        print(f"账户: {accounts[0]}")

    time.sleep(0.5)
    return trader, xtdata

# ============================================================
# 加载目标订单
# ============================================================
def load_orders():
    if not ORDERS_CSV.exists():
        raise FileNotFoundError(f"订单文件不存在: {ORDERS_CSV}\n请先运行 generate_orders.py 生成")

    orders = []
    with open(ORDERS_CSV, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            orders.append({
                "code": r["code"],
                "name": r["name"],
                "target_value": float(r["target_value"]),
                "signal_price": float(r["latest_price"]),
            })
    print(f"加载目标: {len(orders)} 只")
    return orders

# ============================================================
# 查询当前持仓
# ============================================================
def query_positions(trader):
    """返回 {code: {market_value, volume, ...}}"""
    pos = trader.query_stock_positions()
    holdings = {}
    for p in pos:
        if p.market_value > 0:
            holdings[p.stock_code] = {
                "market_value": p.market_value,
                "volume": p.volume,
                "cost": p.open_cost,
            }
    print(f"当前持仓: {len(holdings)} 只, 总市值 ¥{sum(h['market_value'] for h in holdings.values()):,.0f}")
    return holdings

# ============================================================
# 查询资产
# ============================================================
def query_asset(trader):
    asset = trader.query_stock_asset()
    cash = asset.cash if hasattr(asset, 'cash') else 0
    total = asset.total_asset if hasattr(asset, 'total_asset') else 0
    print(f"总资产 ¥{total:,.0f} | 现金 ¥{cash:,.0f}")
    return cash, total

# ============================================================
# 主流程
# ============================================================
def main(dry_run=True):
    trader, xtdata = init_qmt()
    orders = load_orders()
    holdings = query_positions(trader)
    cash, total_asset = query_asset(trader)

    target_codes = set(o["code"] for o in orders)
    held_codes = set(holdings.keys())

    # --- 计算差异 ---
    to_buy = []   # (code, est_amount)
    to_sell = []  # (code, reason)
    to_keep = []  # (code, adjustment)

    for code in target_codes - held_codes:
        if code not in target_codes:
            continue
        # 新买入
        order = next(o for o in orders if o["code"] == code)
        to_buy.append((code, order["target_value"]))

    for code in held_codes - target_codes:
        # 不在目标名单中, 清仓
        to_sell.append((code, "不在名单"))

    for code in target_codes & held_codes:
        # 调权: 偏离超过 30% 才调整 (减少小额交易)
        order = next(o for o in orders if o["code"] == code)
        current = holdings[code]["market_value"]
        target = order["target_value"]
        diff_pct = abs(current / target - 1) if target > 0 else 0

        if diff_pct > 0.30:
            if current > target:
                # 超配, 卖出部分
                to_sell.append((code, f"超配 {diff_pct*100:.0f}%"))
            else:
                to_buy.append((code, target - current))
        else:
            to_keep.append(code)

    # --- 检查风控 ---
    # 单只权重
    for code, amount in to_buy:
        weight = amount / total_asset if total_asset > 0 else 0
        if weight > MAX_WEIGHT:
            print(f"⚠️ {code} 买入后权重 {weight*100:.1f}% > {MAX_WEIGHT*100:.0f}%上限, 请手动处理")
            if not dry_run:
                sys.exit(1)

    # --- 报表 ---
    print(f"\n{'='*50}")
    print(f"调仓计划 ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    print(f"{'='*50}")
    print(f"买入: {len(to_buy)} 只  |  卖出: {len(to_sell)} 只  |  不动: {len(to_keep)} 只")

    if to_sell:
        print(f"\n📉 卖出:")
        total_sell = 0
        for code, reason in to_sell:
            mv = holdings.get(code, {}).get("market_value", 0)
            total_sell += mv
            print(f"  {code}  ¥{mv:,.0f}  ({reason})")
        print(f"  预计回笼: ¥{total_sell:,.0f}")

    if to_buy:
        print(f"\n📈 买入:")
        total_buy = 0
        for code, amount in to_buy:
            # 查实时价
            try:
                tick = xtdata.get_full_tick([code])
                price = tick[code]["lastPrice"] if code in tick else 0
            except:
                price = 0

            if price > 0:
                shares = int(amount / (price * 10)) * 10  # 取整到 10 张
                est_cost = shares * price
            else:
                shares = 0
                est_cost = 0

            total_buy += est_cost
            print(f"  {code}  目标 ¥{amount:,.0f}  现价 ¥{price:.2f}  ≈ {shares}张 ¥{est_cost:,.0f}")

        print(f"\n  预计支出: ¥{total_buy:,.0f}  (现金: ¥{cash:,.0f})")
        if total_buy > cash - MIN_CASH:
            print(f"  ⚠️ 现金不足! (需保留 ¥{MIN_CASH:,})")

    # --- 执行 ---
    if dry_run:
        print(f"\n🟡 DRY RUN — 未实际下单")
        print(f"确认无误后运行: python qmt_execute.py --confirm")
        return

    # 卖出
    for code, reason in to_sell:
        try:
            trader.order_target_value(code, 0)  # 目标市值=0 → 清仓
            print(f"  卖出 {code} — 已下单")
        except Exception as e:
            print(f"  ❌ 卖出 {code} 失败: {e}")
        time.sleep(0.3)

    # 买入 (限价单)
    for code, amount in to_buy:
        try:
            tick = xtdata.get_full_tick([code])
            if code not in tick:
                print(f"  ⚠️ {code} 无行情, 跳过")
                continue

            price = tick[code]["lastPrice"]
            bid1 = tick[code].get("bidPrice", [price])[0]  # 买一价
            limit_price = round(bid1 + PRICE_TICK, 2)

            # 检查涨跌停
            limit_up = tick[code].get("limitUp", 999999)
            limit_down = tick[code].get("limitDown", 0)
            if price >= limit_up * 0.995:
                print(f"  ⚠️ {code} 接近涨停 ¥{price}, 跳过")
                continue
            if price <= limit_down * 1.005:
                print(f"  ⚠️ {code} 接近跌停 ¥{price}, 跳过")
                continue

            shares = int(amount / (price * 10)) * 10
            if shares < 10:
                print(f"  ⚠️ {code} 金额太小 ({amount}), 跳过")
                continue

            # 限价买入
            trader.order(code, shares, xttrader.FIX_ORDER_TYPE_BUY, limit_price)
            print(f"  买入 {code} {shares}张 限价 ¥{limit_price} — 已下单")
        except Exception as e:
            print(f"  ❌ 买入 {code} 失败: {e}")
        time.sleep(0.3)

    # 等 10 秒后核对成交
    print(f"\n等待 10 秒核对成交...")
    time.sleep(10)

    # 核对
    print(f"\n=== 成交核对 ===")
    new_holdings = query_positions(trader)
    new_codes = set(new_holdings.keys())

    missed = target_codes - new_codes
    if missed:
        print(f"⚠️ 未成交: {missed}")
        print("请手动检查或重新运行")
    else:
        print("✅ 持仓匹配, 调仓完成")

    cash2, total2 = query_asset(trader)
    print(f"\n总资产 ¥{total2:,.0f} | 现金 ¥{cash2:,.0f}")

if __name__ == "__main__":
    dry = "--confirm" not in sys.argv
    if dry:
        print("🔍 默认 DRY RUN 模式 (仅对比, 不下单)")
        print("   确认后执行: python qmt_execute.py --confirm\n")
    main(dry_run=dry)
