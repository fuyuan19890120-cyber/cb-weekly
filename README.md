# 可转债双低 · 周度自动化

每周五自动筛选可转债双低组合（排雷版），尾盘前锁定名单，下周持有；GitHub Pages 面板追踪持仓收益与溢价率中枢。

> 策略：双低 = 现价 + 转股溢价率百分点。排除价格 <100 / 评级低于 AA- / 正股连续两年扣非亏损或负债率 >90%。取最低 20 只等权，周五收盘调仓，周度轮动。

## 架构

```
每周五 14:31 (北京时间)    GitHub Actions 自动执行 scripts/weekly_run.py
    ↓                       拉取实时快照 → 结算上周 → 排雷选券 → 写回 data/
    ↓
GitHub Pages 面板 (index.html)  读取 data/ JSON → 渲染持仓表/收益柱/净值曲线/溢价率中枢
```

## 面板地址

部署后：`https://<你的用户名>.github.io/cb-weekly/`

## 手动本地运行

```bash
pip install -r requirements.txt
python scripts/weekly_run.py
```
