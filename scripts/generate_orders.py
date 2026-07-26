# -*- coding: utf-8 -*-
"""Mac 端: 从 cb-weekly 面板读取最新持仓, 对比目标仓位, 生成订单 CSV
   输出文件用于 Windows QMT 执行端读取
   用法: python generate_orders.py [--capital 100000]
"""
import json, csv, sys, argparse
from pathlib import Path
from datetime import datetime

DATA = Path(__file__).resolve().parent.parent / "data"
DEFAULT_CAPITAL = 100_000  # 总资金

def load_target():
    """加载最新一期持仓名单"""
    with open(DATA / "holdings.json", encoding="utf-8") as f:
        records = json.load(f)
    latest = records[-1]
    print(f"信号日期: {latest['date']}")
    print(f"溢价率中枢: {latest['center']:.1f}% (3年分位 {latest['center_pct3y']:.0f}%)")
    print(f"信号灯: {latest['light']}")
    return latest

def generate(capital: float):
    target = load_target()
    n = len(target["holdings"])
    per_bond = capital / n

    orders = []
    for h in target["holdings"]:
        orders.append({
            "code": h["code"],
            "name": h["name"],
            "target_value": round(per_bond, 2),
            "latest_price": h["price"],
            "dl": h["dl"],
        })

    # 写入 CSV (Windows QMT 读取)
    out = DATA / "orders.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["code", "name", "target_value", "latest_price", "dl"])
        w.writeheader()
        w.writerows(orders)

    print(f"\n总资金 ¥{capital:,.0f} | 持仓 {n} 只 | 每只 ¥{per_bond:,.0f}")
    print(f"\n买单 ({len(orders)} 只):")
    for o in orders:
        shares = int(o["target_value"] / (o["latest_price"] * 10)) * 10  # 10张=1手
        est_cost = shares * o["latest_price"]
        print(f"  {o['code']} {o['name']:6s}  目标 ¥{o['target_value']:>8,.0f}  "
              f"≈ {shares}张 ¥{est_cost:,.0f}  双低={o['dl']}")
    print(f"\n订单文件: {out}")
    print("将此文件复制到 Windows QMT 机器后运行 qmt_execute.py")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--capital", type=float, default=DEFAULT_CAPITAL, help=f"总资金 (默认 {DEFAULT_CAPITAL:,})")
    args = p.parse_args()
    generate(args.capital)
