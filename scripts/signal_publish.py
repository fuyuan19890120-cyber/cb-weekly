# -*- coding: utf-8 -*-
"""Mac 端: 读取 holdings.json 最新一期 → 推送到 Redis

   用法:
     python signal_publish.py                  # 推送最新一期
     python signal_publish.py --capital 200000  # 同时生成 orders.csv (按指定资金)
     python signal_publish.py --dry-run         # 只打印信号, 不推送
"""
import json, sys, argparse
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"

# 推迟导入, 没有 redis-py 时 dry-run 仍可工作
redis_config = None


def load_latest():
    with open(DATA / "holdings.json", encoding="utf-8") as f:
        records = json.load(f)
    if not records:
        raise ValueError("holdings.json 为空")
    return records[-1]


def main():
    global redis_config
    p = argparse.ArgumentParser(description="推送双低信号到 Redis")
    p.add_argument("--dry-run", action="store_true", help="只打印, 不推 Redis")
    p.add_argument("--capital", type=float, default=0, help="同时生成 orders.csv (指定总资金)")
    args = p.parse_args()

    latest = load_latest()
    signal = {
        "signal_id": latest["date"],
        "timestamp": latest["date"],
        "center": latest["center"],
        "center_pct3y": latest.get("center_pct3y"),
        "light": latest.get("light"),
        "n_bonds": len(latest["holdings"]),
        "holdings": latest["holdings"],
    }

    print(f"信号日期: {signal['signal_id']}")
    print(f"溢价率中枢: {signal['center']:.1f}%  (3年分位 {signal['center_pct3y']:.0f}%, {signal['light']})")
    print(f"持仓: {signal['n_bonds']} 只")
    for h in signal["holdings"]:
        print(f"  {h['code']} {h['name']:6s}  ¥{h['price']:.2f}  溢价 {h['prem']:.1f}%  双低 {h['dl']:.1f}")

    if args.dry_run:
        print("\n🔍 DRY RUN — 未推送")
        return

    # 推送
    from scripts.redis_config import push_signal
    sid = push_signal(signal)
    print(f"\n✅ 已推送: {sid}")

    # 可选: 生成 orders.csv
    if args.capital > 0:
        import csv
        n = len(latest["holdings"])
        per = args.capital / n
        out = DATA / "orders.csv"
        with open(out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["code", "name", "target_value", "latest_price", "dl"])
            w.writeheader()
            for h in latest["holdings"]:
                w.writerow({
                    "code": h["code"], "name": h["name"],
                    "target_value": round(per, 2), "latest_price": h["price"], "dl": h["dl"],
                })
        print(f"✅ orders.csv 已生成 (资金 ¥{args.capital:,.0f}, 每只 ¥{per:,.0f})")


if __name__ == "__main__":
    main()
