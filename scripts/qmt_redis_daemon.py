# -*- coding: utf-8 -*-
"""Windows QMT 端: 通过 Redis IPC 接收信号并自动执行调仓

  用法:
    python qmt_redis_daemon.py --dry-run              # 拉一次信号, 只对比不下单
    python qmt_redis_daemon.py --once --confirm       # 拉一次信号, 执行后退出
    python qmt_redis_daemon.py --confirm              # 常驻模式, 订阅频道持续监听
    python qmt_redis_daemon.py --poll 60 --confirm    # 轮询模式 (不依赖 Pub/Sub), 每60秒检查一次

  前置: MiniQMT 已登录, xtquant 已安装, redis-py 已安装
  配置: 环境变量 CB_REDIS_URL 或 CB_REDIS_HOST/PORT/PASSWORD/DB (同 redis_config.py)
       CB_CAPITAL 总资金 (默认 100000)
"""
import json, sys, time, os, argparse
from pathlib import Path
from datetime import datetime

DATA = Path(__file__).resolve().parent.parent / "data"
DATA.mkdir(exist_ok=True)

# ============================================================
# 配置
# ============================================================
QMT_PATH = r"C:\Program Files\国金证券QMT"          # MiniQMT 安装路径, 按实际修改
ACCOUNT = ""                                         # 资金账号, 留空自动获取
MIN_CASH = 5_000                                     # 保留现金
PRICE_TICK = 0.01                                    # 买一价加 tick
MAX_WEIGHT = 0.08                                    # 单只上限 8%
DEFAULT_CAPITAL = int(os.environ.get("CB_CAPITAL", "100000"))
LAST_SIGNAL_FILE = DATA / "last_signal.txt"


# ============================================================
# QMT 初始化
# ============================================================
def init_qmt():
    import xtquant.xttrader as xttrader
    from xtquant import xtdata

    xtdata.download_history_data2([], period='1d')

    session = int(time.time())
    trader = xttrader.XtQuantTrader(QMT_PATH, session)
    trader.start()
    if trader.connect() != 0:
        raise RuntimeError("QMT 连接失败, 请检查 MiniQMT 是否已登录")

    if ACCOUNT:
        trader.subscribe(ACCOUNT)
    else:
        accounts = trader.query_accounts()
        if not accounts:
            raise RuntimeError("未找到账户")
        trader.subscribe(accounts[0])
        print(f"账户: {accounts[0]}")

    time.sleep(0.5)
    return trader, xtdata


def query_positions(trader):
    """返回 {code: {market_value, volume, cost}}"""
    pos = trader.query_stock_positions()
    holdings = {}
    for p in pos:
        if p.market_value > 0:
            holdings[p.stock_code] = {
                "market_value": p.market_value,
                "volume": p.volume,
                "cost": p.open_cost,
            }
    return holdings


def query_asset(trader):
    asset = trader.query_stock_asset()
    cash = asset.cash if hasattr(asset, 'cash') else 0
    total = asset.total_asset if hasattr(asset, 'total_asset') else 0
    return cash, total


# ============================================================
# Redis 订阅
# ============================================================
def make_redis():
    from scripts.redis_config import get_redis, KEY_SIGNAL, CHAN_UPDATE
    return get_redis()


def load_capital():
    """从 Redis 读取资金参数 (若未设置环境变量)"""
    r = make_redis()
    val = r.get("cb:config:capital")
    return float(val) if val else DEFAULT_CAPITAL


# ============================================================
# 下单执行 (同 qmt_execute.py 核心逻辑)
# ============================================================
def execute_signal(signal: dict, trader, xtdata, capital: float, dry_run: bool):
    """接收一个信号, 对比持仓, 生成并执行调仓计划"""
    orders = signal["holdings"]
    n = len(orders)
    per_bond = capital / n
    target_map = {o["code"]: per_bond for o in orders}
    target_codes = set(target_map.keys())

    holdings = query_positions(trader)
    held_codes = set(holdings.keys())
    cash, total_asset = query_asset(trader)

    print(f"\n{'='*50}")
    print(f"信号: {signal['signal_id']} | 中枢 {signal['center']}% | {signal['light']}")
    print(f"目标: {n} 只等权 | 当前: {len(holdings)} 只 | 现金 ¥{cash:,.0f}")
    print(f"{'='*50}")

    to_buy, to_sell, to_keep = [], [], []

    for code in target_codes - held_codes:
        to_buy.append((code, per_bond))

    for code in held_codes - target_codes:
        to_sell.append((code, "不在目标名单"))

    for code in target_codes & held_codes:
        current = holdings[code]["market_value"]
        target = per_bond
        diff_pct = abs(current / target - 1) if target > 0 else 0
        if diff_pct > 0.30:
            to_sell.append((code, f"超配 {diff_pct*100:.0f}%")) if current > target else to_buy.append((code, target - current))
        else:
            to_keep.append(code)

    # 风控: 单只权重
    for code, amount in to_buy:
        if amount / total_asset > MAX_WEIGHT:
            print(f"⚠️ {code} 权重超 {MAX_WEIGHT*100:.0f}%, 跳过")
            to_buy.remove((code, amount))

    if to_sell:
        print(f"\n📉 卖出 ({len(to_sell)} 只):")
        for code, reason in to_sell:
            mv = holdings.get(code, {}).get("market_value", 0)
            print(f"  {code}  ¥{mv:,.0f}  ({reason})")

    if to_buy:
        print(f"\n📈 买入 ({len(to_buy)} 只):")
        total_buy = 0
        for code, amount in to_buy:
            try:
                tick = xtdata.get_full_tick([code])
                price = tick[code]["lastPrice"] if code in tick else 0
            except Exception:
                price = 0
            shares = int(amount / (price * 10)) * 10 if price > 0 else 0
            total_buy += shares * price
            print(f"  {code}  目标 ¥{amount:,.0f}  ¥{price:.2f}  ≈ {shares}张")
        print(f"  预计支出 ¥{total_buy:,.0f}  现金 ¥{cash:,.0f}")

    print(f"不动: {len(to_keep)} 只")

    if dry_run:
        print("\n🔍 DRY RUN — 未实际下单")
        return

    # 执行卖出
    for code, reason in to_sell:
        try:
            trader.order_target_value(code, 0)
            print(f"  卖出 {code} — 已下单")
        except Exception as e:
            print(f"  ❌ 卖出 {code} 失败: {e}")
        time.sleep(0.3)

    # 执行买入
    for code, amount in to_buy:
        try:
            tick = xtdata.get_full_tick([code])
            if code not in tick:
                print(f"  ⚠️ {code} 无行情, 跳过"); continue
            price = tick[code]["lastPrice"]
            bid1 = tick[code].get("bidPrice", [price])[0]
            limit_price = round(bid1 + PRICE_TICK, 2)

            limit_up = tick[code].get("limitUp", 999999)
            limit_down = tick[code].get("limitDown", 0)
            if price >= limit_up * 0.995:
                print(f"  ⚠️ {code} 接近涨停, 跳过"); continue
            if price <= limit_down * 1.005:
                print(f"  ⚠️ {code} 接近跌停, 跳过"); continue

            shares = int(amount / (price * 10)) * 10
            if shares < 10:
                print(f"  ⚠️ {code} 金额太小 ({amount}), 跳过"); continue

            import xtquant.xttrader as xttrader
            trader.order(code, shares, xttrader.FIX_ORDER_TYPE_BUY, limit_price)
            print(f"  买入 {code} {shares}张 限价 ¥{limit_price} — 已下单")
        except Exception as e:
            print(f"  ❌ 买入 {code} 失败: {e}")
        time.sleep(0.3)

    # 成交核对
    time.sleep(10)
    new_holdings = query_positions(trader)
    missed = target_codes - set(new_holdings.keys())
    if missed:
        print(f"⚠️ 未成交: {missed}")
    else:
        print("✅ 持仓匹配, 调仓完成")

    cash2, total2 = query_asset(trader)
    print(f"总资产 ¥{total2:,.0f} | 现金 ¥{cash2:,.0f}")


# ============================================================
# 主入口
# ============================================================
def main():
    p = argparse.ArgumentParser(description="QMT Redis 信号执行守护进程")
    p.add_argument("--dry-run", action="store_true", help="只对比, 不下单")
    p.add_argument("--confirm", action="store_true", help="确认执行 (否则默认 dry-run)")
    p.add_argument("--once", action="store_true", help="执行一次后退出")
    p.add_argument("--poll", type=int, default=0, help="轮询间隔(秒), 0=使用 Pub/Sub 订阅")
    p.add_argument("--capital", type=float, default=DEFAULT_CAPITAL, help=f"总资金 (默认 {DEFAULT_CAPITAL:,})")
    args = p.parse_args()

    dry_run = not args.confirm
    if dry_run:
        print("🔍 默认 DRY RUN — 只对比不下单")
        print("   确认执行加 --confirm\n")

    # 连接 QMT
    print("连接 QMT...")
    trader, xtdata = init_qmt()

    # 连接 Redis
    print("连接 Redis...")
    r = make_redis()
    r.ping()
    print("Redis 连接 OK")

    from scripts.redis_config import KEY_SIGNAL, CHAN_UPDATE

    def process_if_new():
        """拉取最新信号, 如果没处理过就执行"""
        body = r.get(KEY_SIGNAL)
        if not body:
            print("⏳ 暂无信号")
            return

        signal = json.loads(body)
        sid = signal["signal_id"]

        # 去重
        last_sid = ""
        if LAST_SIGNAL_FILE.exists():
            last_sid = LAST_SIGNAL_FILE.read_text(encoding="utf-8").strip()
        if sid == last_sid:
            return  # 已处理过

        print(f"\n📡 收到新信号: {sid}")
        capital = load_capital()
        execute_signal(signal, trader, xtdata, capital, dry_run)

        if not dry_run:
            LAST_SIGNAL_FILE.write_text(sid, encoding="utf-8")
            print(f"已记录: {sid}")

    # 先检查是否有待处理信号
    process_if_new()

    if args.once:
        print("\n--once 模式, 退出")
        return

    if args.poll > 0:
        # 轮询模式
        print(f"\n🔄 轮询模式, 每 {args.poll} 秒检查...")
        while True:
            time.sleep(args.poll)
            try:
                process_if_new()
            except Exception as e:
                print(f"轮询异常: {e}")
    else:
        # Pub/Sub 订阅模式
        print(f"\n👂 订阅 {CHAN_UPDATE}, 等待信号...")
        pubsub = r.pubsub()
        pubsub.subscribe(CHAN_UPDATE)
        for msg in pubsub.listen():
            if msg["type"] != "message":
                continue
            print(f"\n📡 频道通知: {msg['data']}")
            time.sleep(1)  # 等 Redis key 落盘
            try:
                process_if_new()
            except Exception as e:
                print(f"执行异常: {e}")


if __name__ == "__main__":
    main()
